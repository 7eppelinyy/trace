"""/ask：基于本地证据检索的解释生成。

不得直接让模型自由联网回答。必须从：
    Event DB / Evidence / Industry Graph / Market Data
中检索证据。

流程：
    Security → Direct Events → 1-hop Industry Events → 2-hop Industry Events
    → 时间距离 → 来源可靠度 → 产业图距离 → 同行共振 → 候选原因排序 → 生成解释

回答必须能够回到 Event ID 和 Evidence。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from trace.db.connection import Database
from trace.db.repositories import EventImpactRepo, EventRepo, RawItemRepo, SecurityRepo
from trace.domain.models import Event, EventImpact, Security
from trace.graph.industry_graph import IndustryGraph

logger = logging.getLogger(__name__)

# 每个证券最多取回的近期影响条数（与旧实现的 direct 上限一致）
_PER_SECURITY_IMPACTS = 20


@dataclass
class AskEvidence:
    event: Event
    impact: EventImpact | None
    relation: str                     # direct / 1-hop / 2-hop
    distance_score: float = 0.0
    evidence_urls: list[str] = field(default_factory=list)


@dataclass
class AskAnswer:
    security: Security
    candidates: list[AskEvidence]
    text: str


class AskEngine:
    def __init__(self, db: Database, graph: IndustryGraph):
        self.db = db
        self.graph = graph
        self.security_repo = SecurityRepo(db)
        self.impact_repo = EventImpactRepo(db)
        self.event_repo = EventRepo(db)
        self.raw_repo = RawItemRepo(db)

    # ------------------------------------------------------------------
    def ask(self, ticker: str, question: str, limit: int = 5) -> AskAnswer | None:
        security = self.security_repo.get_by_ticker(ticker)
        if security is None:
            return None

        # 1) 证券 → 关系/衰减：direct 优先，其次 1-hop，再 2-hop
        #    （同一证券可能从多条路径到达，取最近的那条）
        relations = self._reachable_securities(security)

        # 2) 一次取回所有相关证券的 impact，再一次取回涉及的 event
        #    （此前是：图遍历内层每节点全表扫 security + 逐 impact 一次 get）
        impacts_by_sec = self.impact_repo.list_by_securities(
            list(relations), per_security=_PER_SECURITY_IMPACTS)
        event_ids = [imp.event_id for imps in impacts_by_sec.values() for imp in imps]
        events = self.event_repo.get_many(event_ids)

        # 3) 同 event 去重，保留最高相关度
        best: dict[str, AskEvidence] = {}
        for security_id, (relation, decay) in relations.items():
            for impact in impacts_by_sec.get(security_id, []):
                ev = events.get(impact.event_id)
                if ev is None:
                    continue
                score = impact.final_score * decay
                current = best.get(ev.event_id)
                if current is None or score > current.distance_score:
                    best[ev.event_id] = AskEvidence(
                        event=ev, impact=impact, relation=relation,
                        distance_score=score)

        ranked = sorted(best.values(), key=lambda c: c.distance_score,
                        reverse=True)[:limit]

        # 4) 证据链接只对最终入选的候选取（此前对每个候选都查一次，
        #    绝大多数在排序后被丢弃）
        urls = self.raw_repo.urls_by_events([c.event.event_id for c in ranked])
        for c in ranked:
            c.evidence_urls = urls.get(c.event.event_id, [])

        return AskAnswer(security=security, candidates=ranked,
                         text=self._render(security, question, ranked))

    # ------------------------------------------------------------------
    def _reachable_securities(self, security: Security) -> dict[str, tuple[str, float]]:
        """security_id → (关系, 相关度衰减)，按图距离两跳内展开。

        用 IndustryGraph 预建的 node→securities 倒排；节点按跳数分层去重，
        同一节点不会因为多条路径被重复展开。
        """
        own_nodes = {n.lower() for n in
                     (security.graph_node_ids or [security.ticker.lower()])}

        hop1: set[str] = set()
        for node in own_nodes:
            hop1 |= self.graph.neighbor_nodes(node)
        hop1 -= own_nodes

        hop2: set[str] = set()
        for node in hop1:
            hop2 |= self.graph.neighbor_nodes(node)
        hop2 -= own_nodes | hop1

        relations: dict[str, tuple[str, float]] = {
            security.security_id: ("direct", 1.0)}
        for nodes, relation, decay in ((hop1, "1-hop", 0.8), (hop2, "2-hop", 0.6)):
            for node in nodes:
                for sec in self.graph.securities_at(node):
                    relations.setdefault(sec.security_id, (relation, decay))
        return relations

    def _render(self, security: Security, question: str,
                candidates: list[AskEvidence]) -> str:
        lines = [f"关于 {security.ticker}（{security.company_name_zh}）：{question}", ""]
        if not candidates:
            lines.append("本地事件库中没有找到相关事件证据。")
            return "\n".join(lines)
        lines.append("候选原因（按相关度排序，均来自本地 Event DB 证据）：")
        for i, c in enumerate(candidates, 1):
            direction = c.impact.direction if c.impact else "?"
            lines.append(
                f"{i}. [{c.relation}] {c.event.title}"
                f"（{c.event.event_id}，方向: {direction}，"
                f"相关度: {c.distance_score:.1f}）")
            for url in c.evidence_urls:
                lines.append(f"   证据: {url}")
        lines.append("")
        lines.append("结论均可回溯到上述 Event ID 与原文链接。")
        return "\n".join(lines)
