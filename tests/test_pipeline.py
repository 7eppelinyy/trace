"""流水线测试：run-once 幂等、重复运行去重、生产门禁、Event Revision。

任务书 §13/§14：
    - 同一流水线重复运行：新增重复 RawItem = 0 / Event = 0 / AlertDelivery = 0
    - production 模式禁止 Mock（无 LLM → STOP_LLM_KEY_MISSING；
      无 Telegram → STOP_TELEGRAM_CREDENTIALS_MISSING）
    - same-event revision：material update → version += 1
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trace.alerts.engine import AlertDecision, DeliveryReceipt
from trace.collectors.base import BaseCollector, CollectorRegistry
from trace.common.ids import raw_item_id
from trace.common.modes import (
    STOP_TELEGRAM_CREDENTIALS_MISSING,
    STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING,
)
from trace.domain.models import RawItem, WatchlistEntry
from trace.db.repositories import AlertRuleRepo, SecurityRepo, UserRepo, WatchlistRepo
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine
from trace.pipeline import Pipeline, RunSummary


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """独立临时数据库的 AppContext。

    夹具内先把 LLM Key 置空（占位空串可阻止 .env 中的真实 Key 被
    dotenv 注入），保证测试不依赖任何真实 API Key。
    """
    import trace.app as appmod
    from trace.config import load_config

    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    def fake_load(path=None):
        cfg = load_config()
        cfg.db_path = tmp_path / "pipeline.db"
        return cfg

    monkeypatch.setattr(appmod, "load_config", fake_load)
    return appmod.create_app()


def _us_item() -> RawItem:
    # 使用当前时间：首次同步保护只允许激活后 + 新鲜度窗口内的事件即时推送
    return RawItem(
        raw_item_id=raw_item_id(), source_id="src_sec_edgar",
        source_item_id="edgar:0000723125-26-000042",
        title="Micron Technology Files 8-K: DRAM Contract Price Increase",
        url="https://sec.gov/example/mu-8k",
        published_at=datetime.now(timezone.utc),
        language="en",
        reference="SEC form=8-K accession=0000723125-26-000042")


def _cn_item() -> RawItem:
    return RawItem(
        raw_item_id=raw_item_id(), source_id="src_cninfo",
        source_item_id="src_cninfo:AN688981",
        title="中芯国际：2026年半年度报告",
        url="http://static.cninfo.com.cn/finalpage/example.pdf",
        published_at=datetime.now(timezone.utc),
        language="zh",
        reference="巨潮资讯公告 证券=688981 中芯国际")


class _ScriptedCollector(BaseCollector):
    collector_type = "scripted"

    def __init__(self, db, config, items):
        super().__init__(db, config)
        self._items = items

    @property
    def handled_source_ids(self):
        return {"src_sec_edgar", "src_cninfo"}

    def collect(self):
        return list(self._items)


def _setup_user(app):
    db = app.db
    UserRepo(db).ensure("u1")
    mu = SecurityRepo(db).get_by_ticker("MU")
    smic = SecurityRepo(db).get_by_ticker("688981.SH")
    for sec in (mu, smic):
        WatchlistRepo(db).add(WatchlistEntry(user_id="u1", security_id=sec.security_id))
    AlertRuleRepo(db).set_threshold("u1", None, 1.0)   # 低阈值保证命中
    return mu, smic


def _wire(app, items, sent_box):
    """替换采集器为脚本化采集器 + 捕获投递。"""
    registry = CollectorRegistry()
    registry.register(_ScriptedCollector(app.db, app.config, items))
    app.collectors = registry

    pipeline = Pipeline(app)

    def fake_send(user_id, text, url):
        sent_box.append((user_id, text))
        return DeliveryReceipt(status="sent", chat_id=user_id,
                               message_id=str(len(sent_box)), response="ok")

    pipeline.alert_sender = fake_send
    return pipeline


def test_run_once_then_repeat_is_idempotent(app, monkeypatch):
    """两次相同运行：第二次新增=0、重复=全部、零新投递。"""
    monkeypatch.setenv("TRACE_MODE", "offline")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    mu, smic = _setup_user(app)

    items = [_us_item(), _cn_item()]
    sent: list = []
    pipeline = _wire(app, items, sent)

    s1 = pipeline.run_once()
    assert s1.raw_items_new == 2
    assert s1.raw_items_duplicate == 0
    assert s1.events_created == 2
    assert s1.events_analyzed == 2
    assert s1.alerts_sent >= 2                      # 美股 + A股各至少一条
    us_sent = [t for _, t in sent if "美股" in t]
    cn_sent = [t for _, t in sent if "A股" in t]
    assert us_sent and cn_sent                      # 市场标签区分

    deliveries_after_run1 = app.db.query("SELECT * FROM alert_delivery WHERE status='sent'")
    assert len(deliveries_after_run1) == s1.alerts_sent

    # ---- 第二次：完全相同的输入 ----
    sent.clear()
    s2 = pipeline.run_once()
    assert s2.raw_items_new == 0
    assert s2.raw_items_duplicate == 2              # 全部被确定性去重
    assert s2.events_created == 0
    assert s2.events_revised == 0
    assert s2.alerts_sent == 0                      # 无重复 Telegram 消息
    assert sent == []

    deliveries_after_run2 = app.db.query("SELECT * FROM alert_delivery WHERE status='sent'")
    assert len(deliveries_after_run2) == len(deliveries_after_run1)


def test_run_once_offline_marks_degraded(app, monkeypatch):
    """offline 无 LLM：分析模式必须落库为 rule_based_degraded，行情标记来源。"""
    monkeypatch.setenv("TRACE_MODE", "offline")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _setup_user(app)

    sent: list = []
    pipeline = _wire(app, [_us_item()], sent)
    pipeline.run_once()

    rows = app.db.query("SELECT analysis_mode, market_data_mode FROM event_impact")
    assert rows
    for r in rows:
        assert r["analysis_mode"] == "rule_based_degraded"
        # 行情来源必须显式标记：真实/Mock/不可用，或时段门禁抑制的原因
        # （事件后市场尚未开盘 / 已过反应窗口 → 市场确认取中性，不掺无关涨跌）
        assert r["market_data_mode"] in (
            "mock", "none", "real", "unavailable", "no_quote",
            "market_not_opened_since_event", "reaction_window_expired")


def test_production_without_llm_stops(app, monkeypatch):
    """production 无 LLM Key（默认 Gemini）：整轮停止并返回
    STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING，不得生成伪分析，不得投递。"""
    monkeypatch.setenv("TRACE_MODE", "production")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _setup_user(app)

    sent: list = []
    pipeline = _wire(app, [_us_item()], sent)
    summary = pipeline.run_once()
    assert summary.status == STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING
    assert summary.events_created == 0
    assert summary.alerts_sent == 0
    assert sent == []


def test_production_without_telegram_channel_fails(app, monkeypatch):
    """production 有待投递提醒但没有 Telegram 通道：必须明确失败，不得宣称成功。"""
    monkeypatch.setenv("TRACE_MODE", "production")
    mu, _ = _setup_user(app)

    pipeline = Pipeline(app)
    pipeline.alert_sender = None                    # 没有通道
    from trace.domain.models import Event, EventImpact
    event = Event(event_id="e1", version=1)
    impact = EventImpact(impact_id="IMP-1", event_id="e1",
                         security_id=mu.security_id, final_score=9.0)
    decision = AlertDecision(should_send=True, user_id="u1", event=event,
                             impact=impact, alert_type="new_event")

    summary = RunSummary()
    pipeline._deliver(decision, summary)
    assert summary.status == STOP_TELEGRAM_CREDENTIALS_MISSING
    assert summary.alerts_failed == 1
    assert summary.alerts_sent == 0


# ---------------------------------------------------------------------------
# same-event revision（material update → version += 1）
# ---------------------------------------------------------------------------

def test_revision_material_update_increments_version(db, config):
    """官方来源出现 / 状态升级 → material_update，version+1，允许再次推送。"""
    engine = EventEngine(db, config, embedder=HashEmbedder())
    item1 = RawItem(raw_item_id=raw_item_id(), source_id="src_digitimes",
                    title="Micron may raise DRAM prices",
                    url="https://media.example/1",
                    published_at=datetime.now(timezone.utc), language="en")
    from trace.event_engine.engine import ExtractedEvent
    d1 = engine.ingest(item1, ExtractedEvent(
        title=item1.title, summary=item1.title, entities=["Micron"],
        event_type="pricing", event_status="reported",
        event_time=datetime.now(timezone.utc)))
    assert d1.action == "created"
    event = d1.event
    assert event.version == 1

    item2 = RawItem(raw_item_id=raw_item_id(), source_id="src_sec_edgar",
                    title="Micron 8-K confirms DRAM price increase",
                    url="https://sec.gov/example/confirm",
                    published_at=datetime.now(timezone.utc), language="en")
    result = engine.reviser.apply_update(
        event.event_id, item2, new_status="official_confirmed",
        official_source=True)

    assert result.material_update is True
    assert result.event.version == 2
    assert "rumor_to_confirmed" in result.reasons
    assert result.resend_allowed is True            # 满足再次推送条件


def test_revision_non_material_keeps_version(db, config):
    """仅补充同类证据（无状态/方向/数字变化）不算重大更新。"""
    engine = EventEngine(db, config, embedder=HashEmbedder())
    item1 = RawItem(raw_item_id=raw_item_id(), source_id="src_sec_edgar",
                    title="Micron 8-K A", url="https://sec.gov/a",
                    published_at=datetime.now(timezone.utc), language="en")
    from trace.event_engine.engine import ExtractedEvent
    d1 = engine.ingest(item1, ExtractedEvent(
        title=item1.title, summary=item1.title, entities=["Micron"],
        event_type="earnings", event_status="official_confirmed",
        event_time=datetime.now(timezone.utc)))

    item2 = RawItem(raw_item_id=raw_item_id(), source_id="src_sec_edgar",
                    title="Micron 8-K A exhibit", url="https://sec.gov/b",
                    published_at=datetime.now(timezone.utc), language="en")
    result = engine.reviser.apply_update(d1.event.event_id, item2)
    assert result.material_update is False
    assert result.event.version == 1
    assert result.resend_allowed is False
