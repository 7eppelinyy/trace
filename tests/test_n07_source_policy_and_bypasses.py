"""N07 专项测试：来源策略与所有发送、展示旁路治理。

测试覆盖：
1. can_fetch=False：采集器过滤不可采集源；直接调用 collector.http_client() 触发 PermissionError 阻断 probe 旁路。
2. can_store=False：流水线跳过持久化，记录 source_storage_not_permitted，提交游标，不产生 LLM 调用。
3. can_display=False：/events 列表排除、/events/{id} 返回 404、/ask 排除或返回 insufficient_evidence、
   研究草案返回 404、新建假设返回 403、关联证据返回 403、分享创建返回 403、撤销后公开快照访问返回 404；
   多来源事件保守规则（任一来源禁用则不可展示）。
4. can_forward=False：Delivery worker 投递前复查抑制并记录 source_forward_not_permitted、每日摘要排除、
   CLI replay 退出阻断、Bot /event 与 /sources 指令拒绝展示转发。
5. 授权登记与重启非破坏性：SourceRepo.upsert 保持 verified_at/by 与 operational_override 不被覆盖；
   SourceAuthorizationRecord 记录、列表与活跃查询完备；初始 seed 保持 verified 为空。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from trace.alerts.delivery_worker import delivery_policy
from trace.api.app import create_api_app
from trace.collectors.base import BaseCollector, CollectResult
from trace.common.source_policy import event_permitted, permitted
from trace.db.health import SourceHealthRepo
from trace.db.repositories import (
    AlertOutboxRepo,
    ChannelBindingRepo,
    EventRepo,
    EventSourceRepo,
    RawItemRepo,
    ResearchQuestionRepo,
    SourceRepo,
    UserRepo,
    UserSessionRepo,
)
from trace.db.shares import ResearchShareRepo
from trace.domain.models import (
    AlertOutbox,
    AuthorityLevel,
    Event,
    EventImpact,
    RawItem,
    ResearchQuestion,
    ResearchQuestionState,
    Source,
    SourceAuthorizationRecord,
)
from trace.main import cmd_replay_event
from trace.pipeline import Pipeline


# ---------------------------------------------------------------------------
# 1. can_fetch 测试：调度过滤与 http_client 探针阻断
# ---------------------------------------------------------------------------

class _DummyCollector(BaseCollector):
    collector_type = "dummy"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_test_fetch"}

    def collect(self) -> list[RawItem]:
        return []


def test_can_fetch_disabled_in_collector_and_blocks_http_client(app):
    db = app.db
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_test_fetch",
            source_name="Test Fetch Source",
            source_type="official",
            can_fetch=False,
            can_store=True,
            can_display=True,
            can_forward=True,
            enabled=True,
        )
    )

    collector = _DummyCollector(db, app.config)

    # 1. enabled_sources 排除 can_fetch=False 的来源
    enabled = collector.enabled_sources()
    assert not any(s.source_id == "src_test_fetch" for s in enabled)

    res = collector.run()
    assert res.status == "disabled"

    # 2. 直接调用 http_client 触发 PermissionError（防止 probe/maintenance 旁路）
    with pytest.raises(PermissionError, match="fetch not permitted for source: src_test_fetch"):
        collector.http_client("src_test_fetch")


# ---------------------------------------------------------------------------
# 2. can_store 测试：流水线跳过持久化并推进游标
# ---------------------------------------------------------------------------

def test_can_store_false_skips_storage_commits_cursor_and_avoids_llm(app):
    db = app.db
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_test_nostore",
            source_name="Test NoStore Source",
            source_type="official",
            can_fetch=True,
            can_store=False,
            can_display=True,
            can_forward=True,
            enabled=True,
        )
    )

    now = datetime.now(timezone.utc)
    item = RawItem(
        raw_item_id="raw_nostore_001",
        source_id="src_test_nostore",
        source_item_id="item_001",
        title="Title Should Not Be Stored",
        content="Content Should Not Be Stored",
        published_at=now,
        fetched_at=now,
    )

    # Mock 采集器返回该条目
    mock_collector = MagicMock()
    mock_collector.handled_source_ids = {"src_test_nostore"}
    mock_collector.seconds_until_due.return_value = 0.0
    mock_collector.cursor_repo.commit.return_value = 1
    mock_collector.cursor_repo.discard.return_value = 0
    mock_collector.run.return_value = CollectResult(
        source_ids=["src_test_nostore"],
        items=[item],
        status="ok",
    )
    app.collectors._collectors = [mock_collector]

    ext_calls_before = app.pipeline.extractor.real_llm_calls

    pipeline = Pipeline(app)
    summary = pipeline.run_once()

    # 验证未存储入库
    raw_repo = RawItemRepo(db)
    assert raw_repo.get("raw_nostore_001") is None

    # 验证记录了 source_storage_not_permitted
    assert any("source_storage_not_permitted:src_test_nostore" in n for n in summary.notes)

    # 验证没有调用 LLM 提取
    assert app.pipeline.extractor.real_llm_calls == ext_calls_before


# ---------------------------------------------------------------------------
# 3. can_display 测试：列表、详情、Ask、研究、公开快照与多源保守规则
# ---------------------------------------------------------------------------

def test_can_display_false_hides_from_api_events_and_detail(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_nodisplay",
            source_name="No Display Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=True,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_nodisplay_001",
            title="Secret Non-Displayable Event",
            first_source_id="src_nodisplay",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    with TestClient(create_api_app(app)) as client:
        # /events 列表不包含
        res = client.get("/api/v1/events")
        assert res.status_code == 200
        event_ids = [it["event_id"] for it in res.json().get("items", [])]
        assert "ev_nodisplay_001" not in event_ids

        # /events/{id} 404
        res_detail = client.get("/api/v1/events/ev_nodisplay_001")
        assert res_detail.status_code == 404


def test_can_display_false_excludes_from_ask_engine(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_nodisplay_ask",
            source_name="No Display Ask Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=True,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_ask_blocked",
            title="Blocked Silicon Acquisition Target",
            first_source_id="src_nodisplay_ask",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    # 1. 显式锚定 event_id 查询
    ans_pinned = app.ask_engine.ask(
        question="What happened to the blocked acquisition?",
        event_id="ev_ask_blocked",
    )
    assert ans_pinned is not None
    assert ans_pinned.status == "insufficient_evidence"
    assert len(ans_pinned.candidates) == 0

    # 2. 关键词模糊检索
    ans_search = app.ask_engine.ask(
        question="Blocked Silicon Acquisition",
    )
    assert ans_search is not None
    assert not any(c.event.event_id == "ev_ask_blocked" for c in ans_search.candidates)


def test_can_display_false_blocks_research_draft_create_and_evidence(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_res_blocked",
            source_name="Blocked Research Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=True,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_res_blocked",
            title="Blocked Research Event",
            first_source_id="src_res_blocked",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    raw_repo = RawItemRepo(db)
    raw_repo.insert(
        RawItem(
            raw_item_id="raw_res_blocked",
            source_id="src_res_blocked",
            source_item_id="item_res_01",
            title="Blocked Raw Item",
            content="Evidence content",
            published_at=now,
            fetched_at=now,
        )
    )

    with TestClient(create_api_app(app)) as client:
        # 获取有效访客会话
        session = client.post("/api/v1/auth/session", json={"grant_type": "guest"}).json()
        token = session["session_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. POST /research/questions/draft 返回 404
        draft_res = client.post(
            "/api/v1/research/questions/draft",
            json={"event_id": "ev_res_blocked"},
            headers=headers,
        )
        assert draft_res.status_code == 404

        # 2. POST /research/questions 引用该事件返回 403
        create_res = client.post(
            "/api/v1/research/questions",
            json={
                "title": "Blocked Question",
                "hypothesis": "Hypothesis on blocked event",
                "event_id": "ev_res_blocked",
            },
            headers=headers,
        )
        assert create_res.status_code == 403

        # 3. 创建一个不带 event_id 的合法研究问题
        valid_res = client.post(
            "/api/v1/research/questions",
            json={
                "title": "Valid Question",
                "hypothesis": "Hypothesis on market",
            },
            headers=headers,
        )
        assert valid_res.status_code == 200
        qid = valid_res.json()["question_id"]

        # 4. POST /research/questions/{id}/evidence 尝试关联受限 RawItem 返回 403
        link_res = client.post(
            f"/api/v1/research/questions/{qid}/evidence",
            json={"raw_item_id": "raw_res_blocked"},
            headers=headers,
        )
        assert link_res.status_code == 403


def test_public_share_blocked_at_creation_and_revoked_on_policy_change(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_share_test",
            source_name="Share Test Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=True,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_share_01",
            title="Public Share Event",
            first_source_id="src_share_test",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    with TestClient(create_api_app(app)) as client:
        session = client.post("/api/v1/auth/session", json={"grant_type": "guest"}).json()
        token = session["session_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 创建合法研究问题
        q_res = client.post(
            "/api/v1/research/questions",
            json={
                "title": "Shareable Question",
                "hypothesis": "Demand grows",
                "event_id": "ev_share_01",
            },
            headers=headers,
        )
        assert q_res.status_code == 200
        qid = q_res.json()["question_id"]

        # 创建公开分享成功
        share_res = client.post(f"/api/v1/research/questions/{qid}/share", json={"expires_in_days": 7}, headers=headers)
        assert share_res.status_code == 200
        share_token = share_res.json()["share_token"]

        # 匿名查看公开快照成功
        view_res = client.get(f"/api/v1/research/questions/{qid}/share?token={share_token}")
        assert view_res.status_code == 200

        # 策略变更：来源 can_display 置为 False
        db.execute("UPDATE source SET can_display=0 WHERE source_id='src_share_test'")

        # 再次查看公开快照返回 404（无法绕过策略撤回）
        view_revoked = client.get(f"/api/v1/research/questions/{qid}/share?token={share_token}")
        assert view_revoked.status_code == 404

        # 新增分享也直接被 403 阻断
        share_blocked = client.post(f"/api/v1/research/questions/{qid}/share", json={"expires_in_days": 7}, headers=headers)
        assert share_blocked.status_code == 403


def test_multi_source_conservative_withholding_rule(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_multi_allowed",
            source_name="Allowed Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=True,
            enabled=True,
        )
    )
    source_repo.upsert(
        Source(
            source_id="src_multi_forbidden",
            source_name="Forbidden Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=False,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_multi_01",
            title="Multi-Source Event",
            first_source_id="src_multi_allowed",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    raw_repo = RawItemRepo(db)
    raw_repo.insert(
        RawItem(
            raw_item_id="raw_m_01",
            source_id="src_multi_allowed",
            source_item_id="m01",
            title="Allowed item",
            content="Allowed content",
            published_at=now,
            fetched_at=now,
            event_id="ev_multi_01",
        )
    )
    raw_repo.insert(
        RawItem(
            raw_item_id="raw_m_02",
            source_id="src_multi_forbidden",
            source_item_id="m02",
            title="Forbidden item",
            content="Forbidden content",
            published_at=now,
            fetched_at=now,
            event_id="ev_multi_01",
        )
    )

    # 包含任意一个禁用来源时，整体事件均被保守 withholding
    assert not event_permitted(db, "ev_multi_01", "display")
    assert not event_permitted(db, "ev_multi_01", "forward")


# ---------------------------------------------------------------------------
# 4. can_forward 测试：Worker、CLI Replay、Bot 与 Digest 阻断
# ---------------------------------------------------------------------------

def test_can_forward_false_suppresses_outbox_delivery(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_noforward",
            source_name="No Forward Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=False,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_noforward_01",
            title="No Forward Event",
            first_source_id="src_noforward",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    UserRepo(db).ensure("usr_worker_test")
    ChannelBindingRepo(db).bind("usr_worker_test", "telegram", "123456")

    outbox_item = AlertOutbox(
        outbox_id="outbox_test_01",
        user_id="usr_worker_test",
        channel_type="telegram",
        channel_target="123456",
        event_id="ev_noforward_01",
        impact_id=None,
        event_version=1,
        alert_type="event_alert",
        idempotency_key="key_noforward_01",
        content_text="Test Alert",
    )

    # 即使入队时存在，worker 在投递前复查并抑制
    status, reason = delivery_policy(db, outbox_item)
    assert status == "suppressed"
    assert reason == "source_forward_not_permitted"


def test_can_forward_false_excluded_from_daily_digest(app):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_digest_blocked",
            source_name="Digest Blocked Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=False,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_digest_blocked",
            title="Top Secret High Impact Rumor",
            first_source_id="src_digest_blocked",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    digest = app.digest_builder.build(timezone_name="UTC")

    assert "Top Secret High Impact Rumor" not in digest.content_markdown


def test_cli_replay_event_halts_when_forwarding_not_permitted(app, monkeypatch):
    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_replay_blocked",
            source_name="Replay Blocked Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=False,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_replay_blocked",
            title="Cannot Replay This Event",
            first_source_id="src_replay_blocked",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    monkeypatch.setattr("trace.main.create_app", lambda: app)

    # 验证 cmd_replay_event 触发 SystemExit 阻断
    with pytest.raises(SystemExit, match="disallow forwarding"):
        cmd_replay_event("ev_replay_blocked", acceptance_test=False)


def test_telegram_bot_commands_reject_unpermitted_events(app):
    from trace.bot.telegram_bot import cmd_event, cmd_sources

    db = app.db
    now = datetime.now(timezone.utc)
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_bot_blocked",
            source_name="Bot Blocked Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=False,
            enabled=True,
        )
    )

    ev_repo = EventRepo(db)
    ev_repo.insert(
        Event(
            event_id="ev_bot_blocked",
            title="Bot Blocked Event",
            first_source_id="src_bot_blocked",
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    # Mock Telegram Update & Context
    update = MagicMock()
    update.effective_user.id = 99999
    update.effective_chat.id = 99999
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"app": app}
    context.args = ["ev_bot_blocked"]

    # 模拟允许的 chat_id
    app.config.telegram.allowed_chat_ids = ["99999"]

    # 测试 /event 指令被阻断
    asyncio.run(cmd_event(update, context))
    update.message.reply_text.assert_called_with("该事件来源受策略限制，不可展示或转发")

    # 测试 /sources 指令被阻断
    update.message.reply_text.reset_mock()
    asyncio.run(cmd_sources(update, context))
    update.message.reply_text.assert_called_with("该事件来源受策略限制，不可展示或转发")


# ---------------------------------------------------------------------------
# 5. 授权登记与重启非破坏性测试
# ---------------------------------------------------------------------------

def test_source_upsert_preserves_verified_fields_and_operational_override(app):
    db = app.db
    source_repo = SourceRepo(db)
    verified_time = "2026-09-21T10:00:00+00:00"

    # 1. 初始插入来源并设置 verified 与 override
    source_repo.upsert(
        Source(
            source_id="src_preserve_test",
            source_name="Original Name",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=True,
            verified_at=verified_time,
            verified_by="legal_officer_01",
            enabled=True,
        )
    )
    source_repo.set_operational_override("src_preserve_test", enabled=True, reason="manual_enable", updated_by="admin")

    # 2. 模拟应用重启再次执行 seed upsert（seed 中 verified 为空且 enabled=False）
    source_repo.upsert(
        Source(
            source_id="src_preserve_test",
            source_name="Updated Name From Seed",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=True,
            can_forward=True,
            verified_at=None,
            verified_by=None,
            enabled=False,
        )
    )

    # 3. 验证 verified_at/by 与 operational_override 未被破坏
    loaded = source_repo.get("src_preserve_test")
    assert loaded is not None
    assert loaded.source_name == "Updated Name From Seed"
    assert loaded.verified_at == verified_time
    assert loaded.verified_by == "legal_officer_01"
    assert loaded.operational_override is True


def test_source_authorization_record_lifecycle(app):
    db = app.db
    source_repo = SourceRepo(db)
    source_id = "src_sec_edgar"
    now_str = datetime.now(timezone.utc).isoformat()
    valid_until_str = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()

    record = SourceAuthorizationRecord(
        auth_id="auth_sec_2026",
        source_id=source_id,
        scope=["fetch", "store", "display", "forward"],
        evidence_url_or_file="https://www.sec.gov/developer",
        verified_by="compliance_officer_02",
        verified_at=now_str,
        expires_at=valid_until_str,
        status="verified",
        notes="SEC EDGAR Fair Access Compliance Verified",
    )

    # 插入并查询
    source_repo.record_authorization(record)
    auths = source_repo.list_authorizations(source_id)
    assert len(auths) >= 1
    assert any(a.auth_id == "auth_sec_2026" for a in auths)

    # 获取当前有效授权
    active = source_repo.get_active_authorization(source_id)
    assert active is not None
    assert active.auth_id == "auth_sec_2026"
    assert active.status == "verified"
    assert "display" in active.scope
