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
from trace.event_engine.embeddings import Embedder, cosine, pairwise_cosines


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
        # 时间窗口内事件的 run 级缓存：(events, title_vecs, summary_vecs)。
        # 每轮 run 开始时由 EventEngine.refresh_caches() 失效；本轮内新建/更新的
        # 事件通过 remember() 增量进入，保证同一轮的后续条目仍能与之聚类。
        self._window: tuple[list[Event], list[list[float]], list[list[float]]] | None = None
        self._window_index: dict[str, int] = {}

    # ------------------------------------------------------------------
    def refresh_caches(self) -> None:
        """失效窗口缓存（每轮 run 开始调用）。"""
        self._window = None
        self._window_index = {}

    def remember(self, event: Event, title_vec: list[float] | None = None,
                 summary_vec: list[float] | None = None) -> None:
        """把本轮新建/更新的事件放进窗口缓存。

        必须做：同一轮里前一条 item 刚创建的事件，后一条 item 要能与它聚类，
        否则缓存会把同一事件拆成多个 Event（正确性回归，不只是性能问题）。

        省略向量时从事件自身的 embedding blob 还原。
        """
        if self._window is None:
            return
        if title_vec is None:
            title_vec = _blob_to_vec(event.title_embedding)
        if summary_vec is None:
            summary_vec = _blob_to_vec(event.summary_embedding)
        events, title_vecs, summary_vecs = self._window
        idx = self._window_index.get(event.event_id)
        if idx is None:
            self._window_index[event.event_id] = len(events)
            events.append(event)
            title_vecs.append(title_vec)
            summary_vecs.append(summary_vec)
        else:
            events[idx] = event
            title_vecs[idx] = title_vec
            summary_vecs[idx] = summary_vec

    def _load_window(self) -> tuple[list[Event], list[list[float]], list[list[float]]]:
        """取时间窗口内的事件及其向量（缺失向量懒回填并批量持久化）。

        此前每条新 item 都会重新拉取整个窗口（默认 500 条）并逐条解包 blob；
        一轮 200 条新闻就是 200 次全量拉取。
        """
        if self._window is not None:
            return self._window

        events = self.event_repo.recent(hours=int(self._window_hours))
        title_vecs: list[list[float]] = []
        summary_vecs: list[list[float]] = []
        backfilled: list[Event] = []
        for ev in events:
            changed = False
            tv = _blob_to_vec(ev.title_embedding)
            if not tv:
                tv, blob = self.embed_text(ev.title)
                ev.title_embedding = blob
                changed = True
            sv = _blob_to_vec(ev.summary_embedding)
            if not sv:
                sv, blob = self.embed_text(ev.summary)
                ev.summary_embedding = blob
                changed = True
            if changed:
                backfilled.append(ev)
            title_vecs.append(tv)
            summary_vecs.append(sv)
        if backfilled:
            with self.event_repo.db.transaction():
                for ev in backfilled:
                    self.event_repo.update_embeddings(
                        ev.event_id, ev.title_embedding, ev.summary_embedding)

        self._window = (events, title_vecs, summary_vecs)
        self._window_index = {ev.event_id: i for i, ev in enumerate(events)}
        return self._window

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
        """对时间窗口内的近期 Event 计算 merge_score，按分数降序返回。

        窗口事件与向量按轮缓存（_load_window），再用 batch 余弦算相似度：
        热路径是 每条新 item × 窗口内全部事件 × 2 向量，逐对纯 Python
        点积在事件量增长后会成为瓶颈。
        """
        title_vec, _ = self.embed_text(title)
        summary_vec, _ = self.embed_text(summary)
        events, title_vecs, summary_vecs = self._load_window()

        title_sims = pairwise_cosines(title_vec, title_vecs)
        summary_sims = pairwise_cosines(summary_vec, summary_vecs)

        candidates: list[MatchCandidate] = []
        for ev, t_sim, s_sim in zip(events, title_sims, summary_sims):
            score, breakdown = self._merge_score(
                ev, title, summary, title_vec, summary_vec,
                entities, event_type, event_time,
                title_sim=t_sim, summary_sim=s_sim)
            if score >= self.verifier_min_threshold * 0.8:  # 只对接近阈值的候选保留
                candidates.append(MatchCandidate(event=ev, merge_score=score, breakdown=breakdown))
        candidates.sort(key=lambda c: c.merge_score, reverse=True)
        return candidates

    # ------------------------------------------------------------------
    def _merge_score(self, ev: Event, title: str, summary: str,
                     title_vec: list[float], summary_vec: list[float],
                     entities: list[str], event_type: str,
                     event_time: datetime | None, *,
                     title_sim: float | None = None,
                     summary_sim: float | None = None) -> tuple[float, dict]:
        w = self._weights

        # 向量缺失时懒回填并持久化（find_candidates 已批量处理，
        # 此处兜底直接调用路径）：不落库会每轮重复 embed
        if title_sim is None or summary_sim is None:
            ev_title_vec = _blob_to_vec(ev.title_embedding)
            ev_summary_vec = _blob_to_vec(ev.summary_embedding)
            if not ev_title_vec or not ev_summary_vec:
                if not ev_title_vec:
                    ev_title_vec, blob = self.embed_text(ev.title)
                    ev.title_embedding = blob
                if not ev_summary_vec:
                    ev_summary_vec, blob = self.embed_text(ev.summary)
                    ev.summary_embedding = blob
                self.event_repo.update_embeddings(ev.event_id,
                                                  ev.title_embedding,
                                                  ev.summary_embedding)
            if title_sim is None:
                title_sim = cosine(title_vec, ev_title_vec)
            if summary_sim is None:
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
