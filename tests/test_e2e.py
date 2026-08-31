"""端到端链路测试（不依赖网络）：

    RawItem → Event Engine → 图谱候选 → Stage B 分析 → 集中评分 → Alert Engine
"""

from datetime import datetime, timezone

from trace.ai.pipeline import AnalysisPipeline
from trace.alerts.engine import AlertEngine
from trace.collectors.market_data.alpaca import MockUSMarketProvider
from trace.collectors.market_data.cn import MockCNMarketProvider
from trace.collectors.market_data.confirmation import MarketConfirmer
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem, WatchlistEntry
from trace.db.repositories import AlertRuleRepo, UserRepo, WatchlistRepo, SecurityRepo
from trace.event_engine.embeddings import HashEmbedder
from trace.event_engine.engine import EventEngine, ExtractedEvent
from trace.graph.industry_graph import IndustryGraph


def test_full_chain_event_to_alert(db, config):
    graph = IndustryGraph(db)
    confirmer = MarketConfirmer(MockUSMarketProvider(), MockCNMarketProvider())
    ai = AnalysisPipeline(db, config, graph, confirmer)
    engine = EventEngine(db, config, embedder=HashEmbedder(), verifier=ai.verifier)

    # 用户必须在事件之前注册：首次同步保护要求
    # published_at >= alert_activation_at 的事件才允许即时推送
    user_repo = UserRepo(db)
    user_repo.ensure("u1")
    mu = SecurityRepo(db).get_by_ticker("MU")
    WatchlistRepo(db).add(WatchlistEntry(user_id="u1", security_id=mu.security_id))
    AlertRuleRepo(db).set_threshold("u1", None, 1.0)   # all=1，保证命中

    item = RawItem(
        raw_item_id=raw_item_id(),
        source_id="src_bis",
        source_item_id="bis-test-1",
        title="BIS announces new export restrictions on HBM",
        url="https://bis.example/hbm-rule",
        published_at=datetime.now(timezone.utc),
        language="en",
        content="BIS announces new export restrictions on HBM shipments.",
    )
    extracted = ExtractedEvent(
        title=item.title,
        summary=item.content,
        entities=["BIS", "HBM"],
        event_type="regulation",
        event_status="official_confirmed",
        event_time=datetime.now(timezone.utc),
    )

    decision = engine.ingest(item, extracted)
    assert decision.action == "created"
    event = decision.event

    # Stage B：影响分析（无 LLM → 规则兜底），应能通过图谱找到 MU/SNDK 等
    impacts = ai.analyze_event(event, entity_nodes=["hbm"])
    assert impacts, "图谱应找到受影响证券"
    tickers = set()
    for imp in impacts:
        sec = SecurityRepo(db).get(imp.security_id)
        tickers.add(sec.ticker)
        assert 1 <= imp.final_score <= 10
        assert imp.directness in ("direct", "indirect", "conditional")
    assert "MU" in tickers   # MU 生产 HBM

    # Alert：确认能产生投递决策（用户已在事件前注册，见函数开头）
    alerts = AlertEngine(db, config).evaluate(event, impacts, is_update=False)
    assert any(d.user_id == "u1" for d in alerts.decisions)

    # 幂等：同一 (user, event, security, version, type) 不会重复
    first = next(d for d in alerts.decisions if d.user_id == "u1")
    AlertEngine(db, config).mark_sent(first)
    again = AlertEngine(db, config).evaluate(event, impacts, is_update=False)
    assert not any(d.user_id == "u1" and d.impact.security_id == mu.security_id
                   for d in again.decisions)
