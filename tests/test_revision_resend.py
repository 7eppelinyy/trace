"""事件复推规则测试（alerts.revision_resend_rules）。

settings.yaml 声明了 6 条允许再次推送的修订条件，但长期只有两条能真正触发：
    rumor_to_confirmed / official_source_appeared   ← _merge_into 会传
    direction_changed / key_number_changed
    score_delta_ge_1 / first_cross_threshold        ← 从来没有调用方传值

而且 RevisionResult.resend_allowed 算出来之后没有任何地方读取 —— 配置把某条
规则移出白名单也不会有任何效果。本文件逐条锁定这些规则真的生效。

first_cross_threshold 不需要额外机制：分数低于阈值时根本没有投递记录，
下次跨过阈值时 already_sent 为假，自然会推送（见 test_first_cross_threshold）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trace.alerts.engine import AlertEngine
from trace.common.ids import raw_item_id
from trace.db.repositories import (
    AlertRuleRepo,
    EventImpactRepo,
    EventRepo,
    UserRepo,
    WatchlistRepo,
)
from trace.domain.models import Event, EventImpact, RawItem, WatchlistEntry
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine, ExtractedEvent
from trace.event_engine.revision import EventReviser
from trace.pipeline import Pipeline


def _receipt(sink: list, user_id: str):
    from trace.alerts.engine import DeliveryReceipt
    sink.append(user_id)
    return DeliveryReceipt(status="sent", chat_id=user_id, message_id="1",
                           response="ok")


class _StubConfig:
    """只覆写复推白名单的配置桩。"""

    def __init__(self, rules: list[str], score_delta: float = 1.0):
        self._rules = rules
        self._score_delta = score_delta

    def get(self, path: str, default=None):
        if path == "alerts.revision_resend_rules":
            return self._rules
        if path == "alerts.score_resend_delta":
            return self._score_delta
        return default


def _impact(security_id: str, direction: str, score: float) -> EventImpact:
    return EventImpact(
        impact_id=f"IMP-{security_id}-{direction}", event_id="EV-1",
        security_id=security_id, direction=direction, directness="direct",
        magnitude=6.0, persistence=5.0, directness_score=9.0, confidence=0.8,
        reason="test", source_reliability=9.0, base_score=score, final_score=score)


# ---------------------------------------------------------------------------
# key_number_changed（依赖 event.key_numbers 持久化）
# ---------------------------------------------------------------------------

def _seed_event(db, *, key_numbers: list[str], title="Micron 扩产计划") -> Event:
    now = datetime.now(timezone.utc)
    ev = Event(event_id="EV-KN", title=title, summary=title, event_type="regulation",
               status="reported", version=1, first_seen_at=now, last_updated_at=now,
               event_time=now, first_source_id="src_reuters",
               primary_source_id="src_reuters", all_source_ids=["src_reuters"],
               key_numbers=key_numbers)
    EventRepo(db).insert(ev)
    return ev


def _raw(title: str) -> RawItem:
    now = datetime.now(timezone.utc)
    return RawItem(raw_item_id=raw_item_id(), source_id="src_reuters",
                   source_item_id=f"SI-{title[:8]}", title=title, url="https://x.test/1",
                   published_at=now, language="zh", content=title)


def test_key_numbers_are_persisted(db, config):
    """Stage A 抽到的关键数字必须落库，否则 key_number_changed 无从判断。"""
    engine = EventEngine(db, config, embedder=HashEmbedder())
    engine.refresh_caches()
    decision = engine.ingest(_raw("Micron 宣布投资 100 亿美元扩产"), ExtractedEvent(
        title="Micron 宣布投资 100 亿美元扩产", summary="扩产",
        entities=["Micron"], event_type="regulation", event_status="reported",
        key_numbers=["100亿美元"]))

    stored = EventRepo(db).get(decision.event.event_id)
    assert stored.key_numbers == ["100亿美元"]


def test_key_number_change_is_material(db, config):
    """"投资 100 亿" → "投资 300 亿"：实质更新，版本 +1。"""
    _seed_event(db, key_numbers=["100亿美元"])
    result = EventReviser(db, config).apply_update(
        "EV-KN", _raw("Micron 将投资额提高到 300 亿美元"),
        new_key_numbers=["300亿美元"])

    assert result.material_update
    assert "key_number_changed" in result.reasons
    assert result.event.version == 2
    assert result.resend_allowed                    # 该规则在默认白名单内
    # 新旧数字都保留（后续报道常只提部分数字）
    assert set(result.event.key_numbers) == {"100亿美元", "300亿美元"}


def test_first_time_key_numbers_are_not_a_change(db, config):
    """从"没有数字"到"抽到数字"是证据补充，不是数字变了。

    否则每个事件第一次被修订都会误报为实质更新。
    """
    _seed_event(db, key_numbers=[])
    result = EventReviser(db, config).apply_update(
        "EV-KN", _raw("补充报道：投资 100 亿美元"), new_key_numbers=["100亿美元"])

    assert "key_number_changed" not in result.reasons
    assert not result.material_update
    assert result.event.version == 1
    assert result.event.key_numbers == ["100亿美元"]   # 数字本身仍然记下来


def test_same_key_numbers_are_not_a_change(db, config):
    _seed_event(db, key_numbers=["100亿美元"])
    result = EventReviser(db, config).apply_update(
        "EV-KN", _raw("转载：投资 100 亿美元"), new_key_numbers=["100亿美元"])
    assert "key_number_changed" not in result.reasons


# ---------------------------------------------------------------------------
# direction_changed / score_delta_ge_1（Stage B 之后才判定）
# ---------------------------------------------------------------------------

def test_direction_reversal_detected(db, config):
    reviser = EventReviser(db, config)
    reasons = reviser.analysis_change_reasons(
        [_impact("SEC-US-MU", "bullish", 7.0)],
        [_impact("SEC-US-MU", "bearish", 7.0)])
    assert reasons == ["direction_changed"]


def test_uncertain_to_direction_is_not_a_reversal(db, config):
    """证据补强让方向从 uncertain 变明确，不算"结论反转"。"""
    reviser = EventReviser(db, config)
    assert reviser.analysis_change_reasons(
        [_impact("SEC-US-MU", "uncertain", 7.0)],
        [_impact("SEC-US-MU", "bullish", 7.0)]) == []


def test_score_jump_detected(db, config):
    reviser = EventReviser(db, config)
    assert reviser.analysis_change_reasons(
        [_impact("SEC-US-MU", "bullish", 6.0)],
        [_impact("SEC-US-MU", "bullish", 7.5)]) == ["score_delta_ge_1"]


def test_score_jitter_below_delta_ignored(db, config):
    reviser = EventReviser(db, config)
    assert reviser.analysis_change_reasons(
        [_impact("SEC-US-MU", "bullish", 7.0)],
        [_impact("SEC-US-MU", "bullish", 7.5)]) == []


def test_new_security_alone_is_not_a_change(db, config):
    """图谱多命中一个证券，不代表既有结论变了。"""
    reviser = EventReviser(db, config)
    assert reviser.analysis_change_reasons(
        [_impact("SEC-US-MU", "bullish", 7.0)],
        [_impact("SEC-US-MU", "bullish", 7.0),
         _impact("SEC-US-SNDK", "bearish", 9.0)]) == []


def test_apply_analysis_change_bumps_version(db, config):
    """结论变化必须让 version +1：幂等键随之变化才谈得上再次推送。"""
    ev = _seed_event(db, key_numbers=[])
    result = EventReviser(db, config).apply_analysis_change(ev, ["direction_changed"])

    assert result.event.version == 2
    assert result.material_update and result.resend_allowed
    stored = EventRepo(db).get(ev.event_id)
    assert stored.version == 2
    revisions = db.query("SELECT * FROM event_revision WHERE event_id=?", (ev.event_id,))
    assert len(revisions) == 1
    assert revisions[0]["revision_type"] == "direction_changed"


# ---------------------------------------------------------------------------
# 白名单真的生效
# ---------------------------------------------------------------------------

def test_reason_outside_whitelist_is_not_resendable(db):
    """把 direction_changed 移出白名单后，它就不该再触发推送。"""
    reviser = EventReviser(db, _StubConfig(rules=["rumor_to_confirmed"]))
    result = reviser.apply_analysis_change(
        _seed_event(db, key_numbers=[]), ["direction_changed"])

    assert result.material_update          # 事件确实变了，审计轨迹保留
    assert result.event.version == 2
    assert not result.resend_allowed       # 但不推送


def _setup_watcher(db) -> str:
    user_id = "u-resend"
    UserRepo(db).ensure(user_id, "Asia/Taipei")
    from trace.db.repositories import ChannelBindingRepo
    ChannelBindingRepo(db).bind(user_id,"telegram",user_id)
    WatchlistRepo(db).add(WatchlistEntry(user_id=user_id, security_id="SEC-US-MU"))
    AlertRuleRepo(db).set_threshold(user_id, "SEC-US-MU", 5.0)
    db.execute("UPDATE user SET alert_activation_at=? WHERE user_id=?",
               ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), user_id))
    return user_id


def test_alert_engine_suppresses_when_resend_not_allowed(db, config):
    """resend_allowed=False：达标的更新也不投递，且计入 suppressed。"""
    _setup_watcher(db)
    ev = _seed_event(db, key_numbers=[])
    impacts = [_impact("SEC-US-MU", "bullish", 8.0)]

    engine = AlertEngine(db, config)
    blocked = engine.evaluate(ev, impacts, is_update=True, resend_allowed=False)
    assert blocked.decisions == []
    assert blocked.suppressed == 1

    allowed = engine.evaluate(ev, impacts, is_update=True, resend_allowed=True)
    assert len(allowed.decisions) == 1


def test_new_event_ignores_resend_flag(db, config):
    """首次推送不受复推白名单影响（它不是"再次"推送）。"""
    _setup_watcher(db)
    ev = _seed_event(db, key_numbers=[])
    batch = AlertEngine(db, config).evaluate(
        ev, [_impact("SEC-US-MU", "bullish", 8.0)],
        is_update=False, resend_allowed=False)
    assert len(batch.decisions) == 1


def _pending_confirmation_impact(db, *, base_score: float, final_score: float,
                                 event_time) -> None:
    """造一条"当时拿不到市场确认"的影响（盘后事件的典型状态）。"""
    EventRepo(db).insert(Event(
        event_id="EV-RS", title="Micron 盘后公告扩产", summary="盘后公告",
        event_type="regulation", status="reported", version=1,
        first_seen_at=event_time, last_updated_at=event_time,
        event_time=event_time, first_source_id="src_micron_ir",
        primary_source_id="src_micron_ir", all_source_ids=["src_micron_ir"]))
    EventImpactRepo(db).upsert(EventImpact(
        impact_id="IMP-RS", event_id="EV-RS", security_id="SEC-US-MU",
        direction="bullish", directness="direct", magnitude=6.0, persistence=5.0,
        directness_score=9.0, confidence=0.8, reason="test",
        source_reliability=9.0, base_score=base_score,
        market_confirmation=5.0, final_score=final_score,
        analysis_mode="llm", market_data_mode="market_not_opened_since_event"))


def _rescore_pipeline(app, monkeypatch, confirmation, sent: list) -> Pipeline:
    pipeline = Pipeline(app)
    monkeypatch.setattr(app.collectors, "run_all", lambda **kw: ([], []))
    monkeypatch.setattr(app.confirmer, "confirm", lambda *a, **kw: confirmation)
    pipeline.alert_sender = lambda user_id, text, url: _receipt(sent, user_id)
    return pipeline


def test_rescore_after_market_opens_crosses_threshold(app, monkeypatch):
    """盘后事件在开盘后补算市场确认，跨过阈值 → 首次推送。

    这条路径不需要升版本：此前分数低于阈值，从未投递过，幂等键不拦。
    这正是 first_cross_threshold 的真实形态。
    """
    from trace.collectors.market_data.confirmation import MarketConfirmation
    from trace.collectors.market_data.base import Quote

    user_id = _setup_watcher(app.db)
    AlertRuleRepo(app.db).set_threshold(user_id, "SEC-US-MU", 7.0)
    # base 6.6 + 中性确认 → 6.6（低于阈值 7）；强正向确认(10) → 6.6+0.15*5 = 7.35
    _pending_confirmation_impact(
        app.db, base_score=6.6, final_score=6.6,
        event_time=datetime.now(timezone.utc) - timedelta(hours=10))

    sent: list = []
    summary = _rescore_pipeline(app, monkeypatch, MarketConfirmation(
        score=10.0, quote=Quote(ticker="MU", ts=datetime.now(timezone.utc),
                                last_price=104.0, prev_close=100.0),
        mode="real", change_pct=4.0), sent).run_once()

    assert summary.rescored_events == 1
    impact = EventImpactRepo(app.db).get("EV-RS", "SEC-US-MU")
    assert impact.final_score == 7.35
    assert impact.market_data_mode == "real"
    assert sent == [user_id], "跨过阈值后应当首次推送"
    # 内容没变，只是第一次拿到真实确认 —— 不是"更新"，不该升版本
    assert EventRepo(app.db).get("EV-RS").version == 1


def test_rescore_does_not_resend_already_delivered(app, monkeypatch):
    """已经推送过的事件重算后不得再打扰用户（幂等键拦截）。"""
    from trace.collectors.market_data.confirmation import MarketConfirmation
    from trace.alerts.engine import AlertDecision, DeliveryReceipt

    user_id = _setup_watcher(app.db)      # 阈值 5.0，6.6 分早已达标
    _pending_confirmation_impact(
        app.db, base_score=6.6, final_score=6.6,
        event_time=datetime.now(timezone.utc) - timedelta(hours=10))
    # 模拟上一轮已投递
    app.alert_engine.mark_sent(AlertDecision(
        should_send=True, user_id=user_id,
        event=EventRepo(app.db).get("EV-RS"),
        impact=EventImpactRepo(app.db).get("EV-RS", "SEC-US-MU"),
        alert_type="new_event"), DeliveryReceipt(status="sent", response="ok"))

    sent: list = []
    summary = _rescore_pipeline(app, monkeypatch, MarketConfirmation(
        score=10.0, quote=None, mode="real", change_pct=4.0), sent).run_once()

    assert summary.rescored_events == 1        # 分数确实更新了
    assert EventImpactRepo(app.db).get("EV-RS", "SEC-US-MU").final_score == 7.35
    assert sent == [], "同一事件同一版本不得重复推送"


def test_market_confirmation_alone_cannot_reach_resend_delta(config):
    """量级说明（防止后人误以为重算会触发 score_delta_ge_1）：

    市场确认对分数的影响上限 = market_confirmation_weight × 5，
    默认 0.15×5 = 0.75，低于默认 score_resend_delta = 1.0。
    改动这两个配置的相对大小时，本断言会提醒重新审视重算路径的语义。
    """
    max_shift = float(config.get("scoring.market_confirmation_weight", 0.15)) * 5
    delta = float(config.get("alerts.score_resend_delta", 1.0))
    assert max_shift < delta


def test_rescore_leaves_still_closed_market_alone(app, monkeypatch):
    """市场仍未开盘：保持原样等下一轮，不得把中性确认当成"已确认"。"""
    from trace.collectors.market_data.confirmation import (
        MODE_MARKET_NOT_OPENED, MarketConfirmation)

    _pending_confirmation_impact(
        app.db, base_score=6.6, final_score=6.6,
        event_time=datetime.now(timezone.utc) - timedelta(hours=10))

    sent: list = []
    summary = _rescore_pipeline(app, monkeypatch, MarketConfirmation(
        score=5.0, quote=None, mode=MODE_MARKET_NOT_OPENED), sent).run_once()

    assert summary.rescored_events == 0
    impact = EventImpactRepo(app.db).get("EV-RS", "SEC-US-MU")
    assert impact.market_data_mode == MODE_MARKET_NOT_OPENED
    assert impact.final_score == 6.6
    assert EventRepo(app.db).get("EV-RS").version == 1


def test_first_cross_threshold(db, config):
    """分数首次跨过阈值：此前没有投递记录，幂等键不拦，自然会推。"""
    user_id = _setup_watcher(db)
    AlertRuleRepo(db).set_threshold(user_id, "SEC-US-MU", 7.0)
    ev = _seed_event(db, key_numbers=[])
    engine = AlertEngine(db, config)

    below = engine.evaluate(ev, [_impact("SEC-US-MU", "bullish", 6.0)])
    assert below.decisions == [] and below.suppressed == 1

    above = engine.evaluate(ev, [_impact("SEC-US-MU", "bullish", 7.2)])
    assert len(above.decisions) == 1
