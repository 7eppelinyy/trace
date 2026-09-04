"""Event Engine：RawItem → Event 的完整编排。

流程：
    RawItem
    → Level 1 确定性去重（source_item_id / canonical_url / title hash / content hash）
    → Stage A 事件抽取（提取标题/摘要/实体/类型/状态）
    → Level 2/3 语义聚类（跨语言）
    → 阈值决策：>=0.82 自动合并；0.72–0.82 LLM verifier；<0.72 新 Event
    → Event Revision（新进展不丢失）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from trace.db.connection import Database
from trace.db.repositories import EventRepo, EventSourceRepo, RawItemRepo, SourceRepo
from trace.common.ids import event_id as new_event_id
from trace.domain.models import OFFICIAL_AUTHORITIES, Event, EventSource, RawItem
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import detect_language, normalize_raw_item
from trace.event_engine.revision import EventReviser, RevisionResult
from trace.event_engine.semantic_cluster import SemanticCluster

logger = logging.getLogger(__name__)


@dataclass
class ExtractedEvent:
    """Stage A Event Extractor 的结构化输出。"""
    title: str
    summary: str
    entities: list[str]
    event_type: str
    event_status: str
    event_time: datetime | None = None
    key_numbers: list[str] | None = None
    # 任务书 §8 Stage A 扩展字段
    summary_zh: str = ""
    facts: list[str] | None = None
    uncertainties: list[str] | None = None
    source_claim_type: str = ""
    evidence_ids: list[str] | None = None


class SameEventVerifier(Protocol):
    """LLM same-event verifier：merge_score 在 [0.72, 0.82) 区间时调用。"""

    def is_same_event(self, new: ExtractedEvent, existing: Event) -> bool: ...


class NullVerifier:
    """无 LLM 时的兜底：不合并，创建新 Event。"""

    def is_same_event(self, new: ExtractedEvent, existing: Event) -> bool:
        return False


@dataclass
class EngineDecision:
    action: str                     # "duplicate" | "merged" | "created" | "revised"
    event: Event | None = None
    reason: str = ""


class EventEngine:
    def __init__(self, db: Database, config, *,
                 embedder=None,
                 verifier: SameEventVerifier | None = None):
        self.db = db
        self.config = config
        self.raw_repo = RawItemRepo(db)
        self.event_repo = EventRepo(db)
        self.source_repo = EventSourceRepo(db)
        self.registry = SourceRepo(db)   # source_registry：权威等级判断
        self.exact_dedup = ExactDedup(db)
        if embedder is None:
            from trace.event_engine.embeddings import build_embedder
            embedder = build_embedder(config)
        self.cluster = SemanticCluster(db, embedder, config)
        self.reviser = EventReviser(db, config)
        self.verifier: SameEventVerifier = verifier or NullVerifier()

    # ------------------------------------------------------------------
    def refresh_caches(self) -> None:
        """失效按轮缓存（Pipeline.run_once 每轮开始调用）。"""
        self.cluster.refresh_caches()

    # ------------------------------------------------------------------
    def ingest(self, item: RawItem, extracted: ExtractedEvent, *,
               skip_exact_dedup: bool = False) -> EngineDecision:
        normalize_raw_item(item)

        # Level 1：确定性去重（调用方已检查时可跳过，避免每条重复 4 次查询）
        if not skip_exact_dedup:
            dup = self.exact_dedup.check(item)
            if dup.is_duplicate:
                logger.info("exact dedup hit (%s): %s", dup.reason, item.title)
                return EngineDecision(action="duplicate", reason=dup.reason)

        # Level 2/3：语义聚类（跨语言）
        candidates = self.cluster.find_candidates(
            title=extracted.title, summary=extracted.summary,
            entities=extracted.entities, event_type=extracted.event_type,
            event_time=extracted.event_time)

        if candidates:
            top = candidates[0]
            if top.merge_score >= self.cluster.auto_merge_threshold:
                return self._merge_into(top.event, item)
            if top.merge_score >= self.cluster.verifier_min_threshold:
                if self.verifier.is_same_event(extracted, top.event):
                    return self._merge_into(top.event, item)

        # 创建新 Event
        return self._create_event(item, extracted)

    # ------------------------------------------------------------------
    def _is_official_source(self, source_id: str | None) -> bool:
        """来源权威等级判断（集中配置在 source_registry，不硬编码）。"""
        if not source_id:
            return False
        source = self.registry.get(source_id)
        return bool(source and source.authority_level in OFFICIAL_AUTHORITIES)

    def _merge_into(self, ev: Event, item: RawItem) -> EngineDecision:
        self.raw_repo.insert(item)
        # 官方来源晚于媒体/其他来源出现：官方确认修订（任务书 §8）
        # reported → official_confirmed，event_version += 1，仅 material update 允许再推送
        official = self._is_official_source(item.source_id)
        new_status = ("official_confirmed"
                      if official and ev.status != "official_confirmed" else None)
        result: RevisionResult = self.reviser.apply_update(
            ev.event_id, item, new_status=new_status, official_source=official)
        logger.info("merged into %s (reason=%s, official=%s)",
                    ev.event_id, result.revision_type, official)
        # 窗口缓存里的事件对象换成修订后的版本（标题/摘要未变，向量沿用）
        self.cluster.remember(result.event)
        action = "revised" if result.material_update else "merged"
        return EngineDecision(action=action, event=result.event, reason=result.revision_type)

    def _create_event(self, item: RawItem, extracted: ExtractedEvent) -> EngineDecision:
        self.raw_repo.insert(item)
        now = datetime.now(timezone.utc)
        title_vec, title_blob = self.cluster.embed_text(extracted.title)
        summary_vec, summary_blob = self.cluster.embed_text(extracted.summary)

        ev = Event(
            event_id=new_event_id(),
            title=extracted.title,
            summary=extracted.summary,
            event_type=extracted.event_type or "other",
            status=extracted.event_status or "reported",
            version=1,
            first_seen_at=now,
            last_updated_at=now,
            event_time=extracted.event_time or item.published_at or now,
            language=detect_language(extracted.title + extracted.summary),
            first_source_id=item.source_id,
            primary_source_id=item.source_id,
            all_source_ids=[item.source_id] if item.source_id else [],
            title_embedding=title_blob,
            summary_embedding=summary_blob,
        )
        self.event_repo.insert(ev)
        self.raw_repo.link_event(item.raw_item_id, ev.event_id)
        self.source_repo.add(EventSource(event_id=ev.event_id,
                                         raw_item_id=item.raw_item_id, role="first"))
        # 同一轮的后续 item 必须能与刚建的事件聚类（否则缓存会拆分同一事件）
        self.cluster.remember(ev, title_vec, summary_vec)
        logger.info("created event %s: %s", ev.event_id, ev.title)
        return EngineDecision(action="created", event=ev)
