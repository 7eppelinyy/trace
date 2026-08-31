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

        candidates: list[AskEvidence] = []

        # 1) Direct Events
        for impact in self.impact_repo.list_by_security(security.security_id)[:20]:
            ev = self.event_repo.get(impact.event_id)
            if ev is None:
                continue
            candidates.append(AskEvidence(
                event=ev, impact=impact, relation="direct",
                distance_score=impact.final_score,
                evidence_urls=self._urls(ev.event_id)))

        # 2) 1-hop / 2-hop Industry Events（按图距离扩展）
        for node in security.graph_node_ids or [security.ticker.lower()]:
            for edge in self.graph.neighbors(node):
                other = edge.to_node if edge.from_node == node else edge.from_node
                candidates.extend(self._events_of_node(other, relation="1-hop", decay=0.8))
                for edge2 in self.graph.neighbors(other):
                    node2 = edge2.to_node if edge2.from_node == other else edge2.from_node
                    candidates.extend(self._events_of_node(node2, relation="2-hop", decay=0.6))

        # 排序：distance_score 降序；去重（同 event 保留最高分）
        best: dict[str, AskEvidence] = {}
        for c in candidates:
            key = c.event.event_id
            if key not in best or c.distance_score > best[key].distance_score:
                best[key] = c
        ranked = sorted(best.values(), key=lambda c: c.distance_score, reverse=True)[:limit]

        return AskAnswer(security=security, candidates=ranked,
                         text=self._render(security, question, ranked))

    # ------------------------------------------------------------------
    def _events_of_node(self, node: str, relation: str, decay: float) -> list[AskEvidence]:
        out: list[AskEvidence] = []
        secs = [s for s in self.security_repo.list_all()
                if node in (s.graph_node_ids or [])]
        for sec in secs:
            for impact in self.impact_repo.list_by_security(sec.security_id)[:5]:
                ev = self.event_repo.get(impact.event_id)
                if ev is None:
                    continue
                out.append(AskEvidence(
                    event=ev, impact=impact, relation=relation,
                    distance_score=impact.final_score * decay,
                    evidence_urls=self._urls(ev.event_id)))
        return out

    def _urls(self, event_id: str) -> list[str]:
        return [it.url for it in self.raw_repo.list_by_event(event_id) if it.url][:3]

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
