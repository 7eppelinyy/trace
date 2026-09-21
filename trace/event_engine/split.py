"""Auditable event splitting tool.

Provides transactional separation of mismerged raw_items into an independent Event,
with auditable EventRevision tracking and reason logging.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trace.common.ids import event_id as make_event_id, revision_id
from trace.db.connection import Database
from trace.db.repositories import EventRepo, EventRevisionRepo, EventSourceRepo, RawItemRepo
from trace.domain.models import Event, EventRevision, EventSource

logger = logging.getLogger(__name__)


class EventSplitter:
    def __init__(self, db: Database):
        self.db = db
        self.event_repo = EventRepo(db)
        self.revision_repo = EventRevisionRepo(db)
        self.source_repo = EventSourceRepo(db)
        self.raw_repo = RawItemRepo(db)

    def split_raw_item(self, original_event_id: str, raw_item_id: str,
                       *, reason: str, operator_id: str = "system") -> Event:
        """从 original_event_id 中拆分出 raw_item_id，建立独立的新 Event。

        具备完整可审计日志（EventRevision），原子事务提交。
        """
        orig_ev = self.event_repo.get(original_event_id)
        if not orig_ev:
            raise ValueError(f"original event not found: {original_event_id}")

        raw = self.raw_repo.get(raw_item_id)
        if not raw:
            raise ValueError(f"raw_item not found: {raw_item_id}")

        sources = self.source_repo.list_by_event(original_event_id)
        matching = [s for s in sources if s.raw_item_id == raw_item_id]
        if not matching:
            raise ValueError(f"raw_item {raw_item_id} does not belong to event {original_event_id}")

        now = datetime.now(timezone.utc)
        new_ev_id = make_event_id()

        with self.db.transaction():
            # 1. 创建新 Event
            new_ev = Event(
                event_id=new_ev_id,
                title=raw.title,
                summary=raw.content[:300] if raw.content else raw.title,
                status="reported",
                version=1,
                first_seen_at=raw.published_at or now,
                last_updated_at=now,
                event_time=raw.published_at or now,
                language=raw.language or "zh",
                first_source_id=raw.source_id,
                primary_source_id=raw.source_id,
                all_source_ids=[raw.source_id] if raw.source_id else [],
                material_update=True,
            )
            self.event_repo.insert(new_ev)

            # 2. 从原事件转移 raw_item 链接
            self.raw_repo.link_event(raw_item_id, new_ev_id)
            self.db.execute(
                "DELETE FROM event_source WHERE event_id=? AND raw_item_id=?",
                (original_event_id, raw_item_id),
            )
            self.source_repo.add(EventSource(
                event_id=new_ev_id,
                raw_item_id=raw_item_id,
                role="first",
            ))

            # 3. 记录原事件审计修订
            orig_ev.version += 1
            orig_ev.material_update = True
            orig_ev.last_updated_at = now
            self.event_repo.update(orig_ev)

            self.revision_repo.add(EventRevision(
                revision_id=revision_id(),
                event_id=orig_ev.event_id,
                version=orig_ev.version,
                revision_type="audit_split",
                material_update=True,
                note=f"Split {raw_item_id} into {new_ev_id} by {operator_id}: {reason}",
                created_at=now,
            ))

            # 4. 记录新事件审计修订
            self.revision_repo.add(EventRevision(
                revision_id=revision_id(),
                event_id=new_ev_id,
                version=1,
                revision_type="split_created",
                material_update=True,
                note=f"Created from split of {original_event_id} by {operator_id}: {reason}",
                created_at=now,
            ))

        logger.info("Split %s from %s into %s: %s", raw_item_id, original_event_id, new_ev_id, reason)
        return new_ev
