"""AI 分析流水线：把 Stage A / Stage B / 图谱 / 评分 / 行情确认串起来。

流程：
    Event（含 Evidence）
    → 产业链图谱找候选证券（direct/indirect/conditional 区分）
    → Stage B Impact Analyzer（Structured Output，校验失败不进 Alert）
    → 集中评分引擎（source_reliability + directness + magnitude + persistence）
    → Market Confirmation（行情）
    → 写入 event_impact
"""

from __future__ import annotations

import logging
import uuid

from datetime import datetime, timedelta, timezone

from trace.ai.analyzer import ImpactAnalyzer, directness_score
from trace.ai.budget import LLMBudget
from trace.ai.extractor import EventExtractor
from trace.ai.llm_client import LLMClient
from trace.ai.verifier import LLMSameEventVerifier
from trace.collectors.market_data.confirmation import MarketConfirmer
from trace.db.connection import Database
from trace.db.repositories import (
    EventImpactRepo,
    ForecastSnapshotRepo,
    MarketSnapshotRepo,
    RawItemRepo,
    SecurityRepo,
    SourceRepo,
)
from trace.domain.models import Event, EventImpact, ForecastSnapshot
from trace.graph.industry_graph import IndustryGraph
from trace.scoring.engine import ScoreInput, ScoringEngine
from trace.scoring.signals import detect_supply_demand

logger = logging.getLogger(__name__)


class AnalysisResults(list):
    def __init__(self):
        super().__init__()
        self.snapshots = []


class AnalysisPipeline:
    def __init__(self, db: Database, config, graph: IndustryGraph,
                 confirmer: MarketConfirmer):
        self.db = db
        self.config = config
        self.graph = graph
        self.confirmer = confirmer
        self.scoring = ScoringEngine(config)
        # 成本熔断：每次真实 API 调用（含重试）前检查当日预算
        self.budget = LLMBudget(db, int(config.get("llm.daily_call_budget", 0)),
                               ask_limit=int(config.get("llm.ask_daily_budget", 300)),
                               ask_user_limit=int(config.get("llm.ask_user_daily_budget", 30)),
                               pipeline_reserve=config.get("llm.pipeline_reserved_calls"))
        self.llm = LLMClient(config.llm, budget=self.budget)
        self.extractor = EventExtractor(self.llm, config.llm)
        self.analyzer = ImpactAnalyzer(self.llm, config.llm)
        self.verifier = LLMSameEventVerifier(self.llm, config.llm)
        self.impact_repo = EventImpactRepo(db)
        self.snapshot_repo = ForecastSnapshotRepo(db)
        self.snapshots = MarketSnapshotRepo(db)
        self.raw_repo = RawItemRepo(db)
        self.security_repo = SecurityRepo(db)
        self.source_repo = SourceRepo(db)
        # 每轮 run 级缓存：证券/实体/来源主数据在单轮内不变，
        # 避免逐事件、逐证据重复全表加载（/watch 动态注册由下一轮生效）
        self._securities_cache: list | None = None
        self._entities_cache: list | None = None
        self._sources_cache: dict | None = None

    def refresh_caches(self) -> None:
        """失效每轮缓存（Pipeline.run_once 开始 / replay 前调用）。"""
        self._securities_cache = None
        self._entities_cache = None
        self._sources_cache = None

    def _cached_securities(self) -> list:
        if self._securities_cache is None:
            self._securities_cache = self.security_repo.list_all()
        return self._securities_cache

    def _cached_entities(self) -> list:
        if self._entities_cache is None:
            from trace.db.repositories import EntityAliasRepo
            self._entities_cache = EntityAliasRepo(self.db).all_with_aliases()
        return self._entities_cache

    def _cached_sources(self) -> dict:
        if self._sources_cache is None:
            self._sources_cache = {s.source_id: s
                                   for s in self.source_repo.list_all()}
        return self._sources_cache

    # ------------------------------------------------------------------
    def resolve_entity_nodes(self, entity_names: list[str], *,
                             securities: list | None = None,
                             entities: list | None = None) -> list[str]:
        """把实体名（公司/产品/概念，中英文皆可）映射为产业链图谱节点。

        映射顺序：Security Master（graph_node_ids 或 ticker）→
        entity_alias 表（entity_id）→ 原样小写兜底。
        主数据默认取每轮缓存；调用方可显式传入。
        """
        secs = securities if securities is not None else self._cached_securities()
        ents = entities if entities is not None else self._cached_entities()

        # 别名 → 对象 的倒排映射（与原逐名全表扫描语义一致：先到先得）
        sec_by_alias: dict[str, object] = {}
        for s in secs:
            for cand in (s.ticker, s.company_name_en, s.company_name_zh, *s.aliases):
                if cand:
                    sec_by_alias.setdefault(str(cand).strip().lower(), s)
        ent_by_name: dict[str, str] = {}
        for e in ents:
            ent_by_name.setdefault(e.name.strip().lower(), e.entity_id)
            for a in e.aliases:
                ent_by_name.setdefault(str(a).strip().lower(), e.entity_id)

        nodes: list[str] = []
        for name in entity_names:
            lowered = name.strip().lower()
            sec = sec_by_alias.get(lowered)
            if sec:
                nodes.extend(sec.graph_node_ids or [sec.ticker.lower()])
                continue
            nodes.append(ent_by_name.get(lowered, lowered))
        return nodes

    def analyze_event(self, event: Event, entity_nodes: list[str] | None = None,
                      extra_entities: list[str] | None = None, *, persist: bool = True,
                      evidence_ids: list[str] | None = None) -> list[EventImpact]:
        """对事件执行 Stage B 影响分析并落库。"""
        from trace.graph.industry_graph import GraphHit

        nodes = list(entity_nodes or [])
        # 事件标题/摘要里出现的实体名也尝试映射到图节点
        if extra_entities:
            nodes.extend(self.resolve_entity_nodes(extra_entities))

        hits = self.graph.find_securities_from_entities(nodes, max_hops=3)

        evidence_items = self.raw_repo.list_by_event(event.event_id)
        if evidence_ids is not None:
            evidence_items = [item for item in (self.raw_repo.get(rid) for rid in evidence_ids) if item]
        sources = self._cached_sources()
        sec_by_id = {s.security_id: s for s in self._cached_securities()}

        # security_map 直接关联（任务书 §9）：公司官方源的公告必须直接
        # 关联其登记证券（如 src_sndk_ir → SEC-US-SNDK），不依赖 LLM 猜
        # 股票代码。图谱命中的是 related/indirect 候选，security_map 命中
        # 的是 primary/direct 公司，两者合并后一起进入 Stage B。
        hit_ids = {h.security.security_id for h in hits}
        for item in evidence_items:
            source = sources.get(item.source_id)
            if not source or not source.security_map:
                continue
            for sec_id in source.security_map:
                if sec_id in hit_ids:
                    continue
                security = sec_by_id.get(sec_id)
                if security is None:
                    logger.warning("security_map %s → unknown security %s",
                                   source.source_id, sec_id)
                    continue
                hit_ids.add(sec_id)
                hits.append(GraphHit(security=security, hops=1,
                                     path=[source.source_id, security.ticker],
                                     edge_types=["official_source_map"]))
        hits.sort(key=lambda h: (h.hops, h.security.ticker))

        if not hits:
            logger.info("event %s: no graph hits", event.event_id)
            return []

        # 供需景气信号：事件级（标题+摘要），确定性词表匹配。
        # 只算一次，套用到该事件的所有 impact 上。
        # 注意：这是供需拐点的弱强度代理（对称：偏紧/过剩都识别），
        # 不单独决定方向——方向仍由 Stage B 对每个证券独立判定。
        sd_signal = detect_supply_demand(event.title, event.summary)
        if sd_signal.has_signal:
            logger.info(
                "event %s supply_demand signal: score=%.1f direction=%s "
                "bull=%s bear=%s", event.event_id, sd_signal.score,
                sd_signal.direction, sd_signal.bull_matches, sd_signal.bear_matches)

        impacts_raw = self.analyzer.analyze(event, hits, evidence_items)

        # 来源可靠度：取该事件最权威来源的 base_reliability
        primary = sources.get(event.primary_source_id or "")
        source_reliability = primary.base_reliability if primary else 5.0

        results = AnalysisResults()
        for imp in impacts_raw:
            hit = imp.pop("_hit", None)
            security = hit.security if hit else self.security_repo.get_by_ticker(
                imp.get("security_ticker", ""))
            if security is None:
                continue
            ds = directness_score(imp.get("directness", "conditional"))
            # 市场确认必须锚定事件时间：事件之后市场还没开过盘、或早已过了
            # 反应窗口时，当前报价反映的是与本事件无关的涨跌，一律取中性。
            confirmation = self.confirmer.confirm(
                security.market, security.ticker,
                imp.get("direction", "uncertain"),
                event_time=event.event_time,
                security_id=security.security_id)

            score_out = self.scoring.score(ScoreInput(
                source_reliability=source_reliability,
                directness=ds,
                magnitude=float(imp.get("magnitude", 5.0)),
                persistence=float(imp.get("persistence", 5.0)),
                market_confirmation=confirmation.score,
                supply_demand=sd_signal.score,
            ))

            impact = EventImpact(
                impact_id=f"IMP-{uuid.uuid4().hex[:12]}",
                event_id=event.event_id,
                security_id=security.security_id,
                direction=imp.get("direction", "uncertain"),
                directness=imp.get("directness", "conditional"),
                magnitude=float(imp.get("magnitude", 5.0)),
                persistence=float(imp.get("persistence", 5.0)),
                directness_score=ds,
                confidence=float(imp.get("confidence", 0.3)),
                reason=imp.get("reason_zh") or imp.get("reason", ""),
                industry_path=imp.get("industry_path", "") or (
                    " → ".join(hit.path) if hit else ""),
                evidence_ids=[it.raw_item_id for it in evidence_items[:5]],
                source_reliability=source_reliability,
                base_score=score_out.base_score,
                market_confirmation=confirmation.score,
                final_score=score_out.final_score,
                # 任务书 §7：降级必须显式标记，不得包装成完整真实分析
                analysis_mode=self.analyzer.last_mode,
                # 逐条确认的实际模式（real/mock/unavailable 或休市/过期原因），
                # 比按市场取的 data_mode 更精确
                market_data_mode=confirmation.mode,
                next_eligible_at=confirmation.next_eligible_at,
                expires_at=confirmation.expires_at,
            )
            impact.created_at = datetime.now(timezone.utc)
            results.append(impact)

            # 不可变预测快照 (T13 / F19 / F27): 记录预测生成时点的行情与判断，消除前视偏差
            if impact.direction in ("bullish", "bearish"):
                analysis_now = datetime.now(timezone.utc)
                bench_code = "SPX" if security.market == "US" else "STAR50"
                from trace.collectors.market_data.time_quality import quote_quality
                usable = quote_quality(confirmation.quote, analysis_now) == 'real'
                anchor_price = confirmation.quote.last_price if usable else None
                anchor_ts = confirmation.quote.market_timestamp if usable else None

                bench_snap = self.snapshots.nearest(bench_code, analysis_now, 6.0)
                bench_anchor = bench_snap.last_price if bench_snap else None

                horizon_h = float(self.config.get("forecast.horizon_hours", 24.0))
                model_ver = f'{self.config.llm.provider}:{self.config.llm.model_impact_analyzer}'
                snap = ForecastSnapshot(
                    snapshot_id=f"SNP-{uuid.uuid4().hex[:12]}",
                    impact_id=impact.impact_id,
                    event_id=event.event_id,
                    security_id=security.security_id,
                    event_version=event.version or 1,
                    predicted_direction=impact.direction,
                    predicted_score=impact.final_score,
                    confidence=impact.confidence,
                    model_version=model_ver,
                    market=security.market,
                    analysis_created_at=analysis_now,
                    published_at=analysis_now,
                    anchor_price=anchor_price,
                    anchor_ts=anchor_ts,
                    horizon_hours=horizon_h,
                    due_at=analysis_now + timedelta(hours=horizon_h),
                    benchmark_code=bench_code,
                    benchmark_anchor_price=bench_anchor,
                    status="pending",
                    created_at=analysis_now,
                )
                results.snapshots.append(snap)

        if persist:
            with self.db.transaction():
                self.persist_results(event, results)
        logger.info("event %s analyzed: %d impacts", event.event_id, len(results))
        return results

    def persist_results(self, event, results):
        """Called in the same short transaction as analysis receipt and notification intent."""
        wanted = {impact.security_id for impact in results}
        for old in self.impact_repo.list_by_event(event.event_id):
            if old.security_id not in wanted:
                self.db.execute('DELETE FROM event_impact WHERE impact_id=?', (old.impact_id,))
        for impact in results:
            self.impact_repo.upsert(impact)
        for snapshot in getattr(results, 'snapshots', []):
            self.snapshot_repo.insert(snapshot)
