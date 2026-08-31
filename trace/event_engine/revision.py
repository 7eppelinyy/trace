"""Event Revision：同一事件的新进展管理。

不能错误去重真正的新进展（rumor → confirmed）。
允许重新推送的情形（可配置）：
  - rumor → confirmed
  - 官方来源出现
  - 方向发生变化
  - 关键数字发生重大变化
  - final_score 变化 >= 1
  - 首次跨过用户提醒阈值
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trace.db.connection import Database
from trace.db.repositories import EventRepo, EventRevisionRepo, EventSourceRepo, RawItemRepo
from trace.common.ids import revision_id
from trace.domain.models import Event, EventRevision, EventSource, RawItem

logger = logging.getLogger(__name__)

_STATUS_RANK = {
    "rumor": 0,
    "reported": 1,
    "partially_confirmed": 2,
    "official_confirmed": 3,
}


@dataclass
class RevisionResult:
    event: Event
    revision_type: str
    material_update: bool
    resend_allowed: bool
    reasons: list[str] = field(default_factory=list)


class EventReviser:
    def __init__(self, db: Database, config):
        self.db = db
        self.event_repo = EventRepo(db)
        self.revision_repo = EventRevisionRepo(db)
        self.source_repo = EventSourceRepo(db)
        self.raw_repo = RawItemRepo(db)
        self._resend_rules = set(
            config.get("alerts.revision_resend_rules", []))
        self._score_delta = float(config.get("alerts.score_resend_delta", 1.0))

    def apply_update(self, event_id: str, raw_item: RawItem, *,
                     new_summary: str | None = None,
                     new_status: str | None = None,
                     new_event_type: str | None = None,
                     official_source: bool = False,
                     direction_changed: bool = False,
                     key_number_changed: bool = False,
                     old_score: float | None = None,
                     new_score: float | None = None,
                     crossed_threshold: bool = False) -> RevisionResult:
        ev = self.event_repo.get(event_id)
        if ev is None:
            raise ValueError(f"event not found: {event_id}")

        reasons: list[str] = []

        # rumor_to_confirmed
        old_rank = _STATUS_RANK.get(ev.status, 1)
        new_rank = _STATUS_RANK.get(new_status or ev.status, old_rank)
        rumor_to_confirmed = (
            old_rank <= _STATUS_RANK["reported"]
            and new_rank >= _STATUS_RANK["official_confirmed"]
        )
        if rumor_to_confirmed:
            reasons.append("rumor_to_confirmed")
        if official_source:
            reasons.append("official_source_appeared")
        if direction_changed:
            reasons.append("direction_changed")
        if key_number_changed:
            reasons.append("key_number_changed")
        if (old_score is not None and new_score is not None
                and abs(new_score - old_score) >= self._score_delta):
            reasons.append("score_delta_ge_1")
        if crossed_threshold:
            reasons.append("first_cross_threshold")

        material_update = bool(reasons)

        # 更新事件本体
        now = datetime.now(timezone.utc)
        ev.last_updated_at = now
        ev.material_update = material_update
        if new_status:
            ev.status = new_status
        if new_summary:
            ev.summary = new_summary
        if new_event_type:
            ev.event_type = new_event_type
        if official_source and raw_item.source_id:
            ev.primary_source_id = raw_item.source_id
        if raw_item.source_id and raw_item.source_id not in ev.all_source_ids:
            ev.all_source_ids.append(raw_item.source_id)

        if material_update:
            ev.version += 1

        self.event_repo.update(ev)

        # 关联新证据（外键约束要求 raw_item 先入库；幂等）
        if not self.raw_repo.exists(raw_item.raw_item_id):
            from trace.event_engine.normalize import normalize_raw_item
            self.raw_repo.insert(normalize_raw_item(raw_item))
        self.raw_repo.link_event(raw_item.raw_item_id, ev.event_id)
        # 来源角色（任务书 §8）：
        #   primary    官方来源成为当前最权威证据
        #   confirming 后续来源对既有事件的确认/佐证
        if official_source and ev.primary_source_id == raw_item.source_id:
            role = "primary"
        else:
            role = "confirming"
        self.source_repo.add(EventSource(event_id=ev.event_id,
                                         raw_item_id=raw_item.raw_item_id, role=role))

        revision_type = reasons[0] if reasons else "evidence_added"
        if material_update:
            self.revision_repo.add(EventRevision(
                revision_id=revision_id(),
                event_id=ev.event_id,
                version=ev.version,
                revision_type=revision_type,
                material_update=True,
                note=";".join(reasons),
                created_at=now,
            ))

        resend_allowed = any(r in self._resend_rules for r in reasons)
        return RevisionResult(event=ev, revision_type=revision_type,
                              material_update=material_update,
                              resend_allowed=resend_allowed, reasons=reasons)
