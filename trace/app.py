"""应用组装根（Composition Root）。

把 config / db / graph / scoring / market / ai / alerts 各层装配成
一个 AppContext，供 pipeline 与 Telegram bot 共用。
"""

from __future__ import annotations

from dataclasses import dataclass

from trace.alerts.ask import AskEngine
from trace.alerts.digest import DigestBuilder
from trace.alerts.engine import AlertEngine
from trace.alerts.template import AlertRenderer
from trace.ai.pipeline import AnalysisPipeline
from trace.collectors.base import CollectorRegistry, build_default_registry
from trace.collectors.market_data.alpaca import build_us_provider
from trace.collectors.market_data.cn import build_cn_provider
from trace.collectors.market_data.confirmation import MarketConfirmer, build_confirmer
from trace.config import AppConfig, load_config
from trace.db.connection import Database, get_database
from trace.db.migration import apply_migrations
from trace.db.repositories import MarketSnapshotRepo
from trace.data.seed import load_all_seeds
from trace.event_engine.engine import EventEngine
from trace.feedback.ledger import ForecastLedger
from trace.graph.industry_graph import IndustryGraph
from trace.market_time.calendar import MarketCalendar
from trace.scoring.engine import ScoringEngine


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    graph: IndustryGraph
    calendar: MarketCalendar
    scoring: ScoringEngine
    confirmer: MarketConfirmer
    event_engine: EventEngine
    pipeline: AnalysisPipeline
    collectors: CollectorRegistry
    alert_engine: AlertEngine
    alert_renderer: AlertRenderer
    digest_builder: DigestBuilder
    ask_engine: AskEngine
    ledger: ForecastLedger


def create_app(config_path=None) -> AppContext:
    config = load_config(config_path)
    db = get_database(config.db_path)
    apply_migrations(db)
    load_all_seeds(db)

    graph = IndustryGraph(db)
    calendar = MarketCalendar(config)
    scoring = ScoringEngine(config)
    # 行情确认接交易日历（休市/过期不得把无关涨跌算作市场确认）
    # 与快照仓库（价格历史供事件锚定回测与事后审计）
    confirmer = build_confirmer(
        build_us_provider(), build_cn_provider(), config,
        calendar=calendar, snapshot_repo=MarketSnapshotRepo(db))

    ai_pipeline = AnalysisPipeline(db, config, graph, confirmer)

    event_engine = EventEngine(db, config, verifier=ai_pipeline.verifier)

    return AppContext(
        config=config,
        db=db,
        graph=graph,
        calendar=calendar,
        scoring=scoring,
        confirmer=confirmer,
        event_engine=event_engine,
        pipeline=ai_pipeline,
        collectors=build_default_registry(db, config),
        alert_engine=AlertEngine(db, config),
        alert_renderer=AlertRenderer(db),
        digest_builder=DigestBuilder(db, config, confirmer),
        ask_engine=AskEngine(db, graph),
        ledger=ForecastLedger(db, config, confirmer),
    )
