"""Alert Engine 幂等、Telegram 投递返回解析、消息渲染与时区测试。

任务书 §10/§12/§14：
    - Alert 幂等：(user, event, security, version, alert_type)
    - failed 不占用幂等键，允许下一轮重试；sent 优先
    - Telegram delivery response parse（不得仅以 HTTP 200 判断）
    - Telegram message render（市场/代码/公司名/方向/重要度/置信度/
      状态/为什么重要/来源/用户本地时间 + 降级标记）
    - 时区转换
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from trace.alerts.engine import AlertEngine, AlertDecision, DeliveryReceipt
from trace.alerts.template import AlertRenderer
from trace.bot.delivery import get_me, send_message
from trace.domain.models import AlertDelivery, Event, EventImpact, WatchlistEntry
from trace.db.repositories import AlertRuleRepo, SecurityRepo, UserRepo, WatchlistRepo


# ---------------------------------------------------------------------------
# Telegram delivery response parse
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code: int, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


def _patch_post(monkeypatch, resp_or_exc):
    calls = []

    def fake(url, **kw):
        calls.append((url, kw))
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr("trace.bot.delivery.httpx.post", fake)
    return calls


def test_delivery_ok_parses_message_id(monkeypatch):
    _patch_post(monkeypatch, _Resp(200, {
        "ok": True,
        "result": {"message_id": 42, "chat": {"id": 123}},
    }))
    receipt = send_message("tok", "123", "hello")
    assert receipt.status == "sent"
    assert receipt.message_id == "42"
    assert receipt.chat_id == "123"
    assert receipt.response == "ok"


def test_delivery_api_error_not_http200_success(monkeypatch):
    """ok=false 时即使 HTTP 200 也算失败。"""
    _patch_post(monkeypatch, _Resp(200, {
        "ok": False, "description": "chat not found"}))
    receipt = send_message("tok", "123", "hello")
    assert receipt.status == "failed"
    assert receipt.response == "api_error"
    assert "chat not found" in receipt.error


def test_delivery_missing_message_id_fails(monkeypatch):
    """ok=true 但没有 message_id：不算完整成功。"""
    _patch_post(monkeypatch, _Resp(200, {"ok": True, "result": {"chat": {"id": 1}}}))
    receipt = send_message("tok", "1", "hello")
    assert receipt.status == "failed"
    assert receipt.response == "api_error"


def test_delivery_network_error(monkeypatch):
    _patch_post(monkeypatch, httpx.ConnectError("no route"))
    receipt = send_message("tok", "123", "hello")
    assert receipt.status == "failed"
    assert receipt.response == "network_error"


def test_delivery_429_retries_after_retry_after(monkeypatch):
    """429：按响应 retry_after 等待后重试一次，成功不算 failed。"""
    sleeps: list[float] = []
    monkeypatch.setattr("trace.bot.delivery.time.sleep",
                        lambda s: sleeps.append(s))
    responses = [
        _Resp(429, {"ok": False, "parameters": {"retry_after": 2}}),
        _Resp(200, {"ok": True, "result": {"message_id": 7, "chat": {"id": "123"}}}),
    ]

    def fake(url, **kw):
        return responses.pop(0)

    monkeypatch.setattr("trace.bot.delivery.httpx.post", fake)
    receipt = send_message("tok", "123", "hello")
    assert receipt.status == "sent"
    assert receipt.message_id == "7"
    assert any(s >= 2 for s in sleeps)


def test_delivery_429_exhausted_reports_api_error(monkeypatch):
    """429 重试后仍失败：回执 failed（api_error），不得伪装成功。"""
    monkeypatch.setattr("trace.bot.delivery.time.sleep", lambda s: None)
    resp = _Resp(429, {"ok": False, "parameters": {"retry_after": 1},
                       "description": "Too Many Requests"})
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return resp

    monkeypatch.setattr("trace.bot.delivery.httpx.post", fake)
    receipt = send_message("tok", "123", "hello")
    assert receipt.status == "failed"
    assert len(calls) == 2               # 恰好重试一次


def test_delivery_non_json(monkeypatch):
    _patch_post(monkeypatch, _Resp(502, None))
    receipt = send_message("tok", "1", "hello")
    assert receipt.status == "failed"
    assert receipt.response == "api_error"


def test_delivery_missing_token_no_request(monkeypatch):
    calls = _patch_post(monkeypatch, _Resp(200, {"ok": True, "result": {}}))
    receipt = send_message("", "1", "hello")
    assert receipt.status == "failed"
    assert receipt.response == "no_channel"
    assert calls == []                       # 无 Token 不得发起请求


def test_get_me_parses_identity(monkeypatch):
    def fake(url, **kw):
        return _Resp(200, {"ok": True, "result": {"id": 1, "username": "trace_bot"}})
    monkeypatch.setattr("trace.bot.delivery.httpx.get", fake)
    me = get_me("tok")
    assert me and me["username"] == "trace_bot"


# ---------------------------------------------------------------------------
# Alert 幂等
# ---------------------------------------------------------------------------

def _mk_delivery(user="u1", event="e1", sec="s1", version=1, atype="new_event",
                 status="sent") -> AlertDelivery:
    return AlertDelivery(
        delivery_id=f"d-{user}-{event}-{version}-{status}",
        user_id=user, event_id=event, security_id=sec,
        event_version=version, alert_type=atype,
        final_score=8.0, sent_at=datetime.now(timezone.utc), status=status)


def test_already_sent_counts_only_sent(db, config):
    engine = AlertEngine(db, config)
    assert not engine.delivery_repo.already_sent("u1", "e1", "s1", 1, "new_event")
    engine.delivery_repo.record(_mk_delivery())
    assert engine.delivery_repo.already_sent("u1", "e1", "s1", 1, "new_event")
    # failed 记录不算已投递（允许重试）
    assert not engine.delivery_repo.already_sent("u1", "e1", "s1", 1, "event_update")


def test_failed_delivery_allows_retry(db, config):
    engine = AlertEngine(db, config)
    engine.delivery_repo.record_failed(_mk_delivery(status="failed"))
    # failed 不占用幂等键
    assert not engine.delivery_repo.already_sent("u1", "e1", "s1", 1, "new_event")
    # 之后成功投递 → sent 优先
    engine.delivery_repo.record(_mk_delivery())
    assert engine.delivery_repo.already_sent("u1", "e1", "s1", 1, "new_event")


def test_sent_not_downgraded_by_failed(db, config):
    engine = AlertEngine(db, config)
    engine.delivery_repo.record(_mk_delivery())
    engine.delivery_repo.record_failed(_mk_delivery(status="failed"))
    row = engine.db.query_one(
        "SELECT status FROM alert_delivery WHERE event_id='e1'")
    assert row["status"] == "sent"           # 成功回执优先，不被失败覆盖


def _setup_user_security(db, config):
    UserRepo(db).ensure("u1")
    sec = SecurityRepo(db).get_by_ticker("SNDK")
    WatchlistRepo(db).add(WatchlistEntry(user_id="u1", security_id=sec.security_id))
    AlertRuleRepo(db).set_threshold("u1", None, 1.0)
    return sec


def _event() -> Event:
    return Event(event_id="e1", title="t", summary="s", version=1)


def _impact(security_id, score=8.0) -> EventImpact:
    return EventImpact(impact_id="IMP-1", event_id="e1", security_id=security_id,
                       direction="bullish", directness="direct",
                       final_score=score, confidence=0.8)


def test_evaluate_idempotent_across_versions(db, config):
    """同一 (user, event, security, version, type) 只发一次；新版本允许再发。"""
    sec = _setup_user_security(db, config)
    engine = AlertEngine(db, config)
    ev = _event()

    batch1 = engine.evaluate(ev, [_impact(sec.security_id)])
    assert len(batch1.decisions) == 1
    engine.mark_sent(batch1.decisions[0],
                     DeliveryReceipt(status="sent", message_id="1", response="ok"))

    batch2 = engine.evaluate(ev, [_impact(sec.security_id)])
    assert batch2.decisions == []            # 已投递 → 抑制
    assert batch2.suppressed >= 1

    ev2 = _event()
    ev2.version = 2                          # 事件重大更新（新版本）
    batch3 = engine.evaluate(ev2, [_impact(sec.security_id)], is_update=True)
    assert len(batch3.decisions) == 1


def test_evaluate_threshold_suppression(db, config):
    sec = _setup_user_security(db, config)
    engine = AlertEngine(db, config)
    AlertRuleRepo(db).set_threshold("u1", None, 9.0)
    batch = engine.evaluate(_event(), [_impact(sec.security_id, score=8.0)])
    assert batch.decisions == []
    assert batch.suppressed == 1


def test_evaluate_mute_suppression(db, config):
    sec = _setup_user_security(db, config)
    UserRepo(db).set_mute("u1", datetime(2999, 1, 1, tzinfo=timezone.utc))
    engine = AlertEngine(db, config)
    batch = engine.evaluate(_event(), [_impact(sec.security_id)])
    assert batch.decisions == []
    assert batch.suppressed == 1


# ---------------------------------------------------------------------------
# Telegram message render
# ---------------------------------------------------------------------------

def _render_ctx(db):
    sec = SecurityRepo(db).get_by_ticker("SNDK")
    ev = Event(event_id="e1", title="SanDisk NAND price increase",
               summary="SanDisk raises NAND contract prices",
               status="official_confirmed", version=1,
               first_source_id="src_sec_edgar", primary_source_id="src_sec_edgar",
               first_seen_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
               last_updated_at=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc))
    impact = EventImpact(
        impact_id="IMP-1", event_id="e1", security_id=sec.security_id,
        direction="bullish", directness="direct", magnitude=7.0,
        confidence=0.85, reason="NAND 涨价直接利好存储厂商",
        industry_path="nand → sndk", final_score=8.2,
        analysis_mode="rule_based_degraded", market_data_mode="mock")
    return ev, impact


def test_render_contains_required_fields(db):
    ev, impact = _render_ctx(db)
    text, _ = AlertRenderer(db).render(ev, impact, "Asia/Taipei")
    # 任务书 §12 必须字段
    assert "美股" in text
    assert "SNDK" in text
    assert "闪迪" in text                      # 公司名称
    assert "偏利多" in text                    # 方向
    assert "重要度：8.2" in text
    assert "置信度：85%" in text
    assert "官方已确认" in text                # 事件状态
    assert "为什么重要" in text and "NAND 涨价" in text
    assert "SEC EDGAR" in text                 # 来源名称
    assert "用户本地时间" in text


def test_render_degraded_markers_explicit(db):
    """降级结果不得包装成完整真实分析。"""
    ev, impact = _render_ctx(db)
    text, _ = AlertRenderer(db).render(ev, impact, "Asia/Taipei")
    assert "规则降级" in text
    assert "mock" in text


def test_render_no_degraded_marker_when_full(db):
    ev, impact = _render_ctx(db)
    impact.analysis_mode = "llm"
    impact.market_data_mode = "real"
    text, _ = AlertRenderer(db).render(ev, impact, "Asia/Taipei")
    assert "规则降级" not in text
    assert "模拟数据" not in text


def test_render_timezone_conversion(db):
    """同一 UTC 时间在不同用户时区显示不同本地时间。"""
    ev, impact = _render_ctx(db)
    renderer = AlertRenderer(db)
    taipei, _ = renderer.render(ev, impact, "Asia/Taipei")
    new_york, _ = renderer.render(ev, impact, "America/New_York")
    assert "2026-08-20 20:00" in taipei
    assert "2026-08-20 08:00" in new_york


def test_render_update_header(db):
    ev, impact = _render_ctx(db)
    text, _ = AlertRenderer(db).render(ev, impact, "Asia/Taipei", is_update=True)
    assert "事件更新" in text


def test_render_update_link_from_evidence(db):
    """原文链接来自事件证据。"""
    from trace.common.ids import raw_item_id
    from trace.domain.models import EventSource, RawItem
    from trace.db.repositories import EventRepo, EventSourceRepo, RawItemRepo

    ev, impact = _render_ctx(db)
    EventRepo(db).insert(ev)                    # 外键约束：event 必须先入库
    item = RawItem(raw_item_id=raw_item_id(), source_id="src_sec_edgar",
                   title="SanDisk 8-K", url="https://sec.gov/8k-example")
    RawItemRepo(db).insert(item)
    EventSourceRepo(db).add(EventSource(event_id="e1",
                                        raw_item_id=item.raw_item_id, role="first"))
    _, url = AlertRenderer(db).render(ev, impact, "Asia/Taipei")
    assert url == "https://sec.gov/8k-example"
