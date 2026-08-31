"""Level 2 近似重复 + Level 3 跨语言 Event 聚类。

merge_score =
    0.35 * title_semantic_similarity
  + 0.25 * summary_semantic_similarity
  + 0.20 * entity_overlap
  + 0.10 * event_type_match
  + 0.10 * time_proximity

阈值（来自 settings.yaml，不允许硬编码）：
    >= 0.82      自动合并
    0.72–0.82    交给 LLM same-event verifier
    <  0.72      创建新 Event
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from trace.db.connection import Database
from trace.db.repositories import EventRepo
from trace.domain.models import Event
from trace.event_engine.embeddings import Embedder, cosine


@dataclass
class MatchCandidate:
    event: Event
    merge_score: float
    breakdown: dict


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


class SemanticCluster:
    def __init__(self, db: Database, embedder: Embedder, config):
        self.event_repo = EventRepo(db)
        self.embedder = embedder
        self._weights = config.get("event_engine.merge_weights", {})
        self._thresholds = config.get("event_engine.merge_thresholds", {})
        self._window_hours = float(config.get("event_engine.time_window_hours", 72))

    # ------------------------------------------------------------------
    @property
    def auto_merge_threshold(self) -> float:
        return float(self._thresholds.get("auto_merge", 0.82))

    @property
    def verifier_min_threshold(self) -> float:
        return float(self._thresholds.get("verifier_min", 0.72))

    # ------------------------------------------------------------------
    def embed_text(self, text: str) -> tuple[list[float], bytes]:
        vec = self.embedder.encode([text or ""])[0]
        return vec, _vec_to_blob(vec)

    # ------------------------------------------------------------------
    def find_candidates(self, title: str, summary: str, entities: list[str],
                        event_type: str, event_time: datetime | None,
                        language: str = "") -> list[MatchCandidate]:
        """对时间窗口内的近期 Event 计算 merge_score，按分数降序返回。"""
        title_vec, _ = self.embed_text(title)
        summary_vec, _ = self.embed_text(summary)

        candidates: list[MatchCandidate] = []
        for ev in self.event_repo.recent(hours=int(self._window_hours)):
            score, breakdown = self._merge_score(
                ev, title, summary, title_vec, summary_vec,
                entities, event_type, event_time)
            if score >= self.verifier_min_threshold * 0.8:  # 只对接近阈值的候选保留
                candidates.append(MatchCandidate(event=ev, merge_score=score, breakdown=breakdown))
        candidates.sort(key=lambda c: c.merge_score, reverse=True)
        return candidates

    # ------------------------------------------------------------------
    def _merge_score(self, ev: Event, title: str, summary: str,
                     title_vec: list[float], summary_vec: list[float],
                     entities: list[str], event_type: str,
                     event_time: datetime | None) -> tuple[float, dict]:
        w = self._weights

        ev_title_vec = _blob_to_vec(ev.title_embedding)
        if not ev_title_vec:
            ev_title_vec, blob = self.embed_text(ev.title)
            ev.title_embedding = blob
        ev_summary_vec = _blob_to_vec(ev.summary_embedding)
        if not ev_summary_vec:
            ev_summary_vec, blob = self.embed_text(ev.summary)
            ev.summary_embedding = blob

        title_sim = cosine(title_vec, ev_title_vec)
        summary_sim = cosine(summary_vec, ev_summary_vec)
        entity_overlap = _entity_overlap(entities, ev)
        type_match = 1.0 if (event_type or "other") == (ev.event_type or "other") else 0.0
        time_prox = _time_proximity(event_time, ev)

        score = (
            float(w.get("title_semantic_similarity", 0.35)) * title_sim
            + float(w.get("summary_semantic_similarity", 0.25)) * summary_sim
            + float(w.get("entity_overlap", 0.20)) * entity_overlap
            + float(w.get("event_type_match", 0.10)) * type_match
            + float(w.get("time_proximity", 0.10)) * time_prox
        )
        breakdown = {
            "title_semantic_similarity": round(title_sim, 4),
            "summary_semantic_similarity": round(summary_sim, 4),
            "entity_overlap": round(entity_overlap, 4),
            "event_type_match": type_match,
            "time_proximity": round(time_prox, 4),
        }
        return round(score, 4), breakdown


def _entity_overlap(entities: list[str], ev: Event) -> float:
    """实体重合度：用事件标题/摘要中是否包含候选实体近似计算。"""
    if not entities:
        return 0.0
    haystack = f"{ev.title} {ev.summary}".lower()
    hits = sum(1 for e in entities if e and e.lower() in haystack)
    return min(1.0, hits / len(entities))


def _time_proximity(event_time: datetime | None, ev: Event) -> float:
    """24 小时内为 1.0，72 小时衰减到 0，半衰期近似线性。"""
    a = event_time or datetime.now(timezone.utc)
    b = ev.event_time or ev.first_seen_at or datetime.now(timezone.utc)
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    hours = abs((a - b).total_seconds()) / 3600
    if hours <= 24:
        return 1.0
    if hours >= 72:
        return 0.0
    return 1.0 - (hours - 24) / 48.0
