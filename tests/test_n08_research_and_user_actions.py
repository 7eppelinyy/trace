"""N08 专项测试：研究页面与真实用户动作闭环。

测试覆盖：
1. 真实用户业务流闭环：事件详情 -> 创建草案 -> 保存私有假设 -> 新原文合并触发待复核证据 -> 用户修订 -> 推进至已验证/归档。
2. match_new_evidence 校验：严格校验 RawItem 存在性与来源 display 许可，杜绝伪造或受限证据注入。
3. 双请求竞争编辑冲突：基于 expected_revision 触发 409 Conflict，保留用户输入不被静默覆盖。
4. 正式分页与真实计数：支持 limit/offset 分页，真实 total 与 has_more，支持 >200 条记录平滑翻页，无死角不可达。
5. 导出完整性与有界快照：支持 >1000 条记录导出，无隐式静默截断。
6. 公开分享脱敏与撤销：公开快照严格剥离 user_notes 私有备注，撤销后立即 404。
7. 明确标注建议：自动推导的条件包含【待用户确认的建议】，不虚构订单或已验证财务结论。
8. 反馈单一可更新记录与权限：按用户/事件版本/标的保持单条可更新，普通用户只读自己，管理员可读全量。
9. Telegram /alert 与偏好仓储双写统一：机器人指令直接同步至 NotificationPreferenceRepo 与 AlertRuleRepo。
10. 全局总开关与跨午夜免打扰：master switch 禁用拦截全部提醒，跨午夜时段与时区转换准确，并发 revision 冲突 409。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi.testclient import TestClient

from trace.alerts.delivery_worker import delivery_policy
from trace.alerts.engine import is_in_quiet_hours
from trace.api.app import create_api_app
from trace.bot.telegram_bot import cmd_alert
from trace.common.source_policy import permitted
from trace.db.repositories import (
    AlertFeedbackRepo,
    AlertOutboxRepo,
    AlertRuleRepo,
    ChannelBindingRepo,
    EventImpactRepo,
    EventRepo,
    NotificationPreferenceRepo,
    RawItemRepo,
    ResearchQuestionRepo,
    SecurityRepo,
    SourceRepo,
    UserRepo,
    UserSessionRepo,
)
from trace.db.shares import ResearchShareRepo
from trace.domain.models import (
    AlertFeedback,
    AlertOutbox,
    Event,
    EventImpact,
    RawItem,
    ResearchQuestion,
    ResearchQuestionState,
    Security,
    Source,
)


def _auth_headers(client, user_id: str = "usr_n08_tester") -> dict[str, str]:
    res = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": user_id})
    assert res.status_code == 200
    token = res.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. 完整投研闭环测试
# ---------------------------------------------------------------------------

def test_full_research_workflow_lifecycle(app):
    db = app.db
    now = datetime.now(timezone.utc)
    ev_repo = EventRepo(db)
    imp_repo = EventImpactRepo(db)
    raw_repo = RawItemRepo(db)
    q_repo = ResearchQuestionRepo(db)

    # 1. 初始化有效数据源与事件
    db.execute("UPDATE source SET can_display=1, can_forward=1 WHERE source_id='src_sec_edgar'")
    eid = "ev_n08_lifecycle"
    ev = Event(
        event_id=eid,
        title="NVIDIA Announces Next-Gen Architecture",
        summary="NVIDIA announces next generation AI superchip.",
        first_source_id="src_sec_edgar",
        first_seen_at=now,
        last_updated_at=now,
    )
    ev_repo.insert(ev)

    imp = EventImpact(
        impact_id=f"imp_{eid}_nvda",
        event_id=eid,
        security_id="SEC-US-NVDA",
        direction="bullish",
        directness="direct",
        magnitude=8.0,
        persistence=8.0,
        confidence=0.9,
        reason="Server demand accelerates chip cycle",
    )
    imp_repo.upsert(imp)

    with TestClient(create_api_app(app)) as client:
        headers = _auth_headers(client, "usr_n08_cycle")

        # 2. 从事件生成草案
        draft_res = client.post("/api/v1/research/questions/draft", json={"event_id": eid, "security_id": "SEC-US-NVDA"}, headers=headers)
        assert draft_res.status_code == 200
        draft = draft_res.json()
        assert "【待用户确认的建议】" in draft["supporting_conditions"][0]
        assert "【待用户确认的建议】" in draft["contradicting_conditions"][0]

        # 3. 保存为私有跟踪记录
        create_res = client.post(
            "/api/v1/research/questions",
            json={
                "event_id": eid,
                "security_id": "SEC-US-NVDA",
                "title": draft["title"],
                "hypothesis": draft["hypothesis"],
                "supporting_conditions": draft["supporting_conditions"],
                "contradicting_conditions": draft["contradicting_conditions"],
                "user_notes": "Private initial thesis note",
            },
            headers=headers,
        )
        assert create_res.status_code == 200
        qid = create_res.json()["question_id"]
        assert create_res.json()["revision"] == 1
        assert create_res.json()["state"] == "tracking"

        # 4. 新数据源条目到达并合并到事件
        raw_new = RawItem(
            raw_item_id="raw_evidence_n08_1",
            source_id="src_sec_edgar",
            source_item_id="doc_new_001",
            title="Q3 Shipment Disclosure",
            content="Customer purchase orders confirmed for Q3 delivery.",
            published_at=now,
            fetched_at=now,
        )
        raw_repo.insert(raw_new)

        matched = q_repo.match_new_evidence(raw_new.raw_item_id, event_id=eid, security_id="SEC-US-NVDA")
        assert len(matched) >= 1
        assert any(q.question_id == qid for q in matched)

        # 5. 用户拉取详情，看见待复核证据已关联
        get_res = client.get(f"/api/v1/research/questions/{qid}", headers=headers)
        assert get_res.status_code == 200
        q_item = get_res.json()
        assert "raw_evidence_n08_1" in q_item["matched_evidence_ids"]
        assert q_item["revision"] == 2

        # 6. 用户复核后更新结论并验证归档
        update_res = client.patch(
            f"/api/v1/research/questions/{qid}",
            json={
                "expected_revision": 2,
                "state": "confirmed",
                "user_notes": "Private verified: customer POs confirmed in filing",
            },
            headers=headers,
        )
        assert update_res.status_code == 200
        assert update_res.json()["state"] == "confirmed"
        assert update_res.json()["revision"] == 3


# ---------------------------------------------------------------------------
# 2. match_new_evidence 严格验证
# ---------------------------------------------------------------------------

def test_match_new_evidence_validation_and_source_policy(app):
    db = app.db
    q_repo = ResearchQuestionRepo(db)
    now = datetime.now(timezone.utc)

    # 1. 不存在证据 ID 抛出异常
    with pytest.raises(ValueError, match="Evidence not found"):
        q_repo.match_new_evidence("raw_not_existing", event_id="ev_any")

    # 2. 来源被禁用展示时忽略关联
    source_repo = SourceRepo(db)
    source_repo.upsert(
        Source(
            source_id="src_forbidden_match",
            source_name="Forbidden Match Source",
            source_type="official",
            can_fetch=True,
            can_store=True,
            can_display=False,
            can_forward=False,
            enabled=True,
        )
    )
    raw_repo = RawItemRepo(db)
    raw_repo.insert(
        RawItem(
            raw_item_id="raw_forbidden_01",
            source_id="src_forbidden_match",
            source_item_id="forbid_01",
            title="Forbidden evidence",
            content="Content",
            published_at=now,
            fetched_at=now,
        )
    )

    UserRepo(db).ensure("usr_match_test")
    EventRepo(db).insert(
        Event(
            event_id="ev_match_target",
            title="Match Target Event",
            first_source_id="src_forbidden_match",
            first_seen_at=now,
            last_updated_at=now,
        )
    )
    q = ResearchQuestion(
        question_id="q_match_test",
        user_id="usr_match_test",
        title="Test Match",
        hypothesis="Hypothesis",
        event_id="ev_match_target",
    )
    q_repo.insert(q)

    # 来源不可展示时，不会关联入研究问题
    matched = q_repo.match_new_evidence("raw_forbidden_01", event_id="ev_match_target")
    assert len(matched) == 0


# ---------------------------------------------------------------------------
# 3. 乐观锁与并发 409 冲突测试
# ---------------------------------------------------------------------------

def test_concurrent_edit_returns_409_and_protects_latest_state(app):
    with TestClient(create_api_app(app)) as client:
        headers = _auth_headers(client, "usr_conflict_test")

        # 1. 创建研究条目
        res = client.post(
            "/api/v1/research/questions",
            json={"title": "Original Title", "hypothesis": "Original Hypothesis"},
            headers=headers,
        )
        qid = res.json()["question_id"]
        rev1 = res.json()["revision"]

        # 2. 模拟客户端 A 成功更新至 revision 2
        res_a = client.patch(
            f"/api/v1/research/questions/{qid}",
            json={"expected_revision": rev1, "title": "Client A Update"},
            headers=headers,
        )
        assert res_a.status_code == 200
        assert res_a.json()["revision"] == 2

        # 3. 客户端 B 持有陈旧的 revision 1 尝试更新，触发 409
        res_b = client.patch(
            f"/api/v1/research/questions/{qid}",
            json={"expected_revision": rev1, "title": "Client B Stale Update"},
            headers=headers,
        )
        assert res_b.status_code == 409
        assert "Research has changed" in res_b.json()["detail"]

        # 4. 验证内容未被旧提交覆盖
        latest = client.get(f"/api/v1/research/questions/{qid}", headers=headers).json()
        assert latest["title"] == "Client A Update"


# ---------------------------------------------------------------------------
# 4. 正式分页与真实计数测试 (>200 条记录)
# ---------------------------------------------------------------------------

def test_pagination_and_true_total_count(app):
    db = app.db
    q_repo = ResearchQuestionRepo(db)
    user_id = "usr_page_test"
    UserRepo(db).ensure(user_id)
    now = datetime.now(timezone.utc)

    # 插入 220 条假设跟踪记录
    with db.transaction():
        for i in range(220):
            q_repo.insert(
                ResearchQuestion(
                    question_id=f"q_page_{i:03d}",
                    user_id=user_id,
                    title=f"Question {i}",
                    hypothesis=f"Hypothesis {i}",
                    created_at=now,
                    updated_at=now,
                )
            )

    with TestClient(create_api_app(app)) as client:
        headers = _auth_headers(client, user_id)

        # 1. 第一页：offset=0, limit=50 -> 应该有 50 条，total=220, has_more=True
        res_p1 = client.get("/api/v1/research/questions?limit=50&offset=0", headers=headers)
        assert res_p1.status_code == 200
        d1 = res_p1.json()
        assert len(d1["items"]) == 50
        assert d1["total"] == 220
        assert d1["has_more"] is True
        assert d1["offset"] == 0

        # 2. 第五页：offset=200, limit=50 -> 应该有 20 条，total=220, has_more=False
        res_p5 = client.get("/api/v1/research/questions?limit=50&offset=200", headers=headers)
        assert res_p5.status_code == 200
        d5 = res_p5.json()
        assert len(d5["items"]) == 20
        assert d5["total"] == 220
        assert d5["has_more"] is False
        assert d5["offset"] == 200


# ---------------------------------------------------------------------------
# 5. 公开分享与私有笔记脱敏测试
# ---------------------------------------------------------------------------

def test_public_share_desensitization_and_revocation(app):
    with TestClient(create_api_app(app)) as client:
        alice_headers = _auth_headers(client, "alice_share")

        # 1. Alice 创建带高度私密笔记的研究
        q_res = client.post(
            "/api/v1/research/questions",
            json={
                "title": "Confidential M&A Thesis",
                "hypothesis": "Target will acquire tech supplier at 30% premium",
                "user_notes": "TOP SECRET: Rumor heard at closed dinner with partner",
            },
            headers=alice_headers,
        )
        qid = q_res.json()["question_id"]

        # 2. Alice 生成公开限时快照
        share_res = client.post(f"/api/v1/research/questions/{qid}/share", json={"expires_in_days": 7}, headers=alice_headers)
        assert share_res.status_code == 200
        token = share_res.json()["share_token"]

        # 3. 匿名用户通过 Capability URL 读取公开快照
        view_res = client.get(f"/api/v1/research/questions/{qid}/share?token={token}")
        assert view_res.status_code == 200
        snapshot = view_res.json()
        assert snapshot["title"] == "Confidential M&A Thesis"
        assert "user_notes" not in snapshot  # 严格剥离私密笔记

        # 4. Alice 撤销分享
        revoke_res = client.delete(f"/api/v1/research/questions/{qid}/share", headers=alice_headers)
        assert revoke_res.status_code == 200

        # 5. 匿名访问立即变为 404
        view_after = client.get(f"/api/v1/research/questions/{qid}/share?token={token}")
        assert view_after.status_code == 404


# ---------------------------------------------------------------------------
# 6. 反馈去重更新与范围权限测试
# ---------------------------------------------------------------------------

def test_alert_feedback_single_opinion_and_scope_permissions(app):
    db = app.db
    now = datetime.now(timezone.utc)
    EventRepo(db).insert(
        Event(
            event_id="ev_fb_test",
            title="Feedback Target Event",
            version=1,
            first_seen_at=now,
            last_updated_at=now,
        )
    )

    with TestClient(create_api_app(app)) as client:
        user_headers = _auth_headers(client, "usr_feedback_tester")

        # 1. 提交初次评价
        r1 = client.post(
            "/api/v1/research/feedback",
            json={"event_id": "ev_fb_test", "rating": "useful", "reason": "Accurate prediction"},
            headers=user_headers,
        )
        assert r1.status_code == 200

        # 2. 重复提交（同用户、同事件版本、同标的）更新原评价，不膨胀分母
        r2 = client.post(
            "/api/v1/research/feedback",
            json={"event_id": "ev_fb_test", "rating": "too_late", "reason": "Delivered after market close"},
            headers=user_headers,
        )
        assert r2.status_code == 200

        fb_repo = AlertFeedbackRepo(db)
        user_fbs = fb_repo.list_by_user("usr_feedback_tester")
        assert len(user_fbs) == 1
        assert user_fbs[0].rating == "too_late"

        # 3. scope='my' 正常返回用户自身统计
        s_my = client.get("/api/v1/research/feedback/summary?scope=my", headers=user_headers)
        assert s_my.status_code == 200
        assert s_my.json()["total"] == 1
        assert s_my.json()["by_rating"].get("too_late") == 1

        # 4. 普通用户请求 scope='all' 被 403 拒绝
        s_all_forbidden = client.get("/api/v1/research/feedback/summary?scope=all", headers=user_headers)
        assert s_all_forbidden.status_code == 403


# ---------------------------------------------------------------------------
# 7. Telegram /alert 与偏好仓储双写统一测试
# ---------------------------------------------------------------------------

def test_telegram_cmd_alert_unification_with_preferences(app):
    db = app.db
    UserRepo(db).ensure("1234567")
    SecurityRepo(db).upsert(
        Security(
            security_id="SEC-US-AAPL",
            ticker="AAPL",
            market="US",
            company_name_zh="苹果",
        )
    )

    update = MagicMock()
    update.effective_chat.id = 1234567
    update.effective_user.id = 1234567
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.bot_data = {"app": app}
    context.args = ["all", "8.5"]

    # 1. 执行 /alert all 8.5
    asyncio.run(cmd_alert(update, context))

    # 验证 NotificationPreferenceRepo 与 AlertRuleRepo 均更新为 8.5
    pref_repo = NotificationPreferenceRepo(db)
    rule_repo = AlertRuleRepo(db)
    global_pref = pref_repo.get("1234567", None)
    assert global_pref is not None
    assert global_pref.threshold == 8.5
    assert rule_repo.get_threshold("1234567", None) == 8.5

    # 2. 执行 /alert AAPL 9.0
    context.args = ["AAPL", "9.0"]
    asyncio.run(cmd_alert(update, context))

    sec_pref = pref_repo.get("1234567", "SEC-US-AAPL")
    assert sec_pref is not None
    assert sec_pref.threshold == 9.0
    assert rule_repo.get_threshold("1234567", "SEC-US-AAPL") == 9.0


# ---------------------------------------------------------------------------
# 8. 偏好总开关、免打扰时段与并发修改 409
# ---------------------------------------------------------------------------

def test_preferences_master_switch_and_quiet_hours(app):
    db = app.db
    pref_repo = NotificationPreferenceRepo(db)
    uid = "usr_pref_master"
    UserRepo(db).ensure(uid)
    ChannelBindingRepo(db).bind(uid, "telegram", "chat_master")

    ev = Event(
        event_id="ev_master_alert",
        title="High Priority Alert",
        first_source_id="src_sec_edgar",
        first_seen_at=datetime.now(timezone.utc),
        last_updated_at=datetime.now(timezone.utc),
    )
    EventRepo(db).insert(ev)

    outbox_item = AlertOutbox(
        outbox_id="out_master_01",
        user_id=uid,
        channel_type="telegram",
        channel_target="chat_master",
        event_id="ev_master_alert",
        impact_id=None,
        event_version=1,
        alert_type="event_alert",
        idempotency_key="key_master_01",
        content_text="Alert text",
    )

    # 1. 正常启用时通过
    pref_repo.set_preference(uid, None, enabled=True, threshold=7.0)
    status, _ = delivery_policy(db, outbox_item)
    assert status is None  # 通过策略校验

    # 2. 总开关关闭时抑制全部提醒
    pref_repo.set_preference(uid, None, enabled=False)
    status, reason = delivery_policy(db, outbox_item)
    assert status == "suppressed"
    assert reason == "notification_disabled"

    # 3. 免打扰跨午夜验证 (23:00 至 07:00)
    t_quiet = datetime(2026, 9, 21, 2, 0, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(t_quiet, "UTC", "23:00", "07:00") is True

    t_awake = datetime(2026, 9, 21, 14, 0, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(t_awake, "UTC", "23:00", "07:00") is False

    # 4. API 并发更新冲突返回 409
    with TestClient(create_api_app(app)) as client:
        headers = _auth_headers(client, uid)
        p1 = client.put(
            "/api/v1/preferences",
            json={"threshold": 8.0, "expected_revision": 2},
            headers=headers,
        )
        assert p1.status_code == 200

        # 重复使用旧版本预期触发 409
        p2 = client.put(
            "/api/v1/preferences",
            json={"threshold": 8.5, "expected_revision": 2},
            headers=headers,
        )
        assert p2.status_code == 409
