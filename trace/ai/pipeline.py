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

from trace.ai.analyzer import ImpactAnalyzer, directness_score
from trace.ai.extractor import EventExtractor
from trace.ai.llm_client import LLMClient
from trace.ai.verifier import LLMSameEventVerifier
from trace.collectors.market_data.confirmation import MarketConfirmer
from trace.db.connection import Database
from trace.db.repositories import (
    EventImpactRepo,
    RawItemRepo,
    SecurityRepo,
    SourceRepo,
)
from trace.domain.models import Event, EventImpact
from trace.graph.industry_graph import IndustryGraph
from trace.scoring.engine import ScoreInput, ScoringEngine

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    def __init__(self, db: Database, config, graph: IndustryGraph,
                 confirmer: MarketConfirmer):
        self.db = db
        self.config = config
        self.graph = graph
        self.confirmer = confirmer
        self.scoring = ScoringEngine(config)
        self.llm = LLMClient(config.llm)
        self.extractor = EventExtractor(self.llm, config.llm)
        self.analyzer = ImpactAnalyzer(self.llm, config.llm)
        self.verifier = LLMSameEventVerifier(self.llm, config.llm)
        self.impact_repo = EventImpactRepo(db)
        self.raw_repo = RawItemRepo(db)
        self.security_repo = SecurityRepo(db)
        self.source_repo = SourceRepo(db)

    # ------------------------------------------------------------------
    def resolve_entity_nodes(self, entity_names: list[str]) -> list[str]:
        """把实体名（公司/产品/概念，中英文皆可）映射为产业链图谱节点。

        映射顺序：Security Master（graph_node_ids 或 ticker）→
        entity_alias 表（entity_id）→ 原样小写兜底。
        """
        from trace.db.repositories import EntityAliasRepo
        entities = EntityAliasRepo(self.db).all_with_aliases()
        nodes: list[str] = []
        for name in entity_names:
            sec = self.security_repo.find_by_alias(name)
            if sec:
                nodes.extend(sec.graph_node_ids or [sec.ticker.lower()])
                continue
            lowered = name.strip().lower()
            node = None
            for ent in entities:
                if lowered == ent.name.lower() or \
                        lowered in [a.lower() for a in ent.aliases]:
                    node = ent.entity_id
                    break
            nodes.append(node or lowered)
        return nodes

    def analyze_event(self, event: Event, entity_nodes: list[str] | None = None,
                      extra_entities: list[str] | None = None) -> list[EventImpact]:
        """对事件执行 Stage B 影响分析并落库。"""
        from trace.graph.industry_graph import GraphHit

        nodes = list(entity_nodes or [])
        # 事件标题/摘要里出现的实体名也尝试映射到图节点
        if extra_entities:
            nodes.extend(self.resolve_entity_nodes(extra_entities))

        hits = self.graph.find_securities_from_entities(nodes, max_hops=3)

        evidence_items = self.raw_repo.list_by_event(event.event_id)

        # security_map 直接关联（任务书 §9）：公司官方源的公告必须直接
        # 关联其登记证券（如 src_sndk_ir → SEC-US-SNDK），不依赖 LLM 猜
        # 股票代码。图谱命中的是 related/indirect 候选，security_map 命中
        # 的是 primary/direct 公司，两者合并后一起进入 Stage B。
        hit_ids = {h.security.security_id for h in hits}
        for item in evidence_items:
            source = self.source_repo.get(item.source_id)
            if not source or not source.security_map:
                continue
            for sec_id in source.security_map:
                if sec_id in hit_ids:
                    continue
                security = self.security_repo.get(sec_id)
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

        impacts_raw = self.analyzer.analyze(event, hits, evidence_items)

        # 来源可靠度：取该事件最权威来源的 base_reliability
        primary = self.source_repo.get(event.primary_source_id or "")
        source_reliability = primary.base_reliability if primary else 5.0

        results: list[EventImpact] = []
        for imp in impacts_raw:
            hit = imp.pop("_hit", None)
            security = hit.security if hit else self.security_repo.get_by_ticker(
                imp.get("security_ticker", ""))
            if security is None:
                continue
            ds = directness_score(imp.get("directness", "conditional"))
            market_score, quote = self.confirmer.score_for(
                security.market, security.ticker, imp.get("direction", "uncertain"))

            score_out = self.scoring.score(ScoreInput(
                source_reliability=source_reliability,
                directness=ds,
                magnitude=float(imp.get("magnitude", 5.0)),
                persistence=float(imp.get("persistence", 5.0)),
                market_confirmation=market_score,
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
                market_confirmation=market_score,
                final_score=score_out.final_score,
                # 任务书 §7：降级必须显式标记，不得包装成完整真实分析
                analysis_mode=self.analyzer.last_mode,
                market_data_mode=self.confirmer.data_mode(security.market),
            )
            self.impact_repo.upsert(impact)
            results.append(impact)

        logger.info("event %s analyzed: %d impacts", event.event_id, len(results))
        return results
