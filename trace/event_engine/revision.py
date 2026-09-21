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
from trace.domain.models import Event, EventImpact, EventRevision, EventSource, RawItem

logger = logging.getLogger(__name__)

_STATUS_RANK = {
    "rumor": 0,
    "reported": 1,
    "partially_confirmed": 2,
    "official_confirmed": 3,
    "contradicted": 4,
    "retracted": 5,
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
                     new_title: str | None = None, document_changed: bool = False,
                     new_summary: str | None = None,
                     new_status: str | None = None,
                     new_event_type: str | None = None,
                     new_key_numbers: list[str] | None = None,
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
        if document_changed:
            reasons.append('document_corrected')

        # 关键数字变化（"投资 100 亿" → "投资 300 亿"）：调用方给出新抽取结果时
        # 由本模块判定，不再要求调用方自己比对
        if new_key_numbers is not None and not key_number_changed:
            key_number_changed = _key_numbers_changed(ev.key_numbers, new_key_numbers)

        # rumor_to_confirmed
        old_rank = _STATUS_RANK.get(ev.status, 1)
        new_rank = _STATUS_RANK.get(new_status or ev.status, old_rank)
        rumor_to_confirmed = (
            old_rank <= _STATUS_RANK["reported"]
            and new_rank >= _STATUS_RANK["official_confirmed"]
        )
        if rumor_to_confirmed:
            reasons.append("rumor_to_confirmed")
        if official_source and raw_item.source_id not in ev.all_source_ids:
            reasons.append("official_source_appeared")

        # 官方否认或撤回属于重大实质修订（F11 / F14）
        if new_status in ("contradicted", "retracted") and new_status != ev.status:
            reasons.append(f"status_{new_status}")
            reasons.append("denial_or_retraction")

        # 官方否认/撤回后再次获得官方确认（恢复确认状态机）
        if ev.status in ("contradicted", "retracted") and new_status == "official_confirmed":
            reasons.append("recovery_to_confirmed")
            reasons.append("rumor_to_confirmed")

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
        if new_title and new_title != ev.title:
            ev.title = new_title
            ev.title_embedding = None
            ev.embedding_model = None
        if new_summary and material_update and new_summary != ev.summary:
            ev.summary = new_summary
            ev.summary_embedding = None
            ev.embedding_model = None
        if new_event_type:
            ev.event_type = new_event_type
        if new_key_numbers:
            # 合并而非覆盖：后续报道往往只提到部分数字
            ev.key_numbers = sorted({*ev.key_numbers, *new_key_numbers})
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

    # ------------------------------------------------------------------
    def analysis_change_reasons(self, previous: list[EventImpact],
                                current: list[EventImpact]) -> list[str]:
        """比对同一事件前后两次 Stage B 结果，给出实质变化原因。

        direction_changed / score_delta_ge_1 这两条复推规则依赖新分数与新方向，
        而修订发生在 Stage B **之前** —— 那时它们还不存在。所以必须在分析之后
        补一次判定，否则这两条规则永远是死的（配置里写着，实际从不触发）。

        只看两次都出现的证券：新增证券本身不算"结论变了"。
        """
        if not previous or not current:
            return []
        before = {i.security_id: i for i in previous}
        reasons: set[str] = set()
        for imp in current:
            old = before.get(imp.security_id)
            if old is None:
                continue
            if old.direction != imp.direction and "uncertain" not in (
                    old.direction, imp.direction):
                # uncertain ↔ 明确方向 属于证据补强，不当作方向反转
                reasons.add("direction_changed")
            if abs(imp.final_score - old.final_score) >= self._score_delta:
                reasons.add("score_delta_ge_1")
        return sorted(reasons)

    def apply_analysis_change(self, event: Event, reasons: list[str]) -> RevisionResult:
        """Stage B 结论实质变化 → 事件版本 +1（幂等键随之变化，允许再次推送）。"""
        now = datetime.now(timezone.utc)
        event.version += 1
        event.material_update = True
        event.last_updated_at = now
        self.event_repo.update(event)

        revision_type = reasons[0] if reasons else "analysis_changed"
        self.revision_repo.add(EventRevision(
            revision_id=revision_id(),
            event_id=event.event_id,
            version=event.version,
            revision_type=revision_type,
            material_update=True,
            note=";".join(reasons),
            created_at=now,
        ))
        logger.info("event %s analysis changed (%s) → version %d",
                    event.event_id, ",".join(reasons), event.version)
        return RevisionResult(
            event=event, revision_type=revision_type, material_update=True,
            resend_allowed=any(r in self._resend_rules for r in reasons),
            reasons=reasons)


def _key_numbers_changed(old: list[str], new: list[str]) -> bool:
    """关键数字是否发生实质变化。

    只在**两边都有**数字时比较：从无到有是证据补充（第一次抽到数字），
    不是"数字变了"；否则每个事件第一次被修订都会误报。
    """
    old_set = {_normalize_number(x) for x in old if str(x).strip()}
    new_set = {_normalize_number(x) for x in new if str(x).strip()}
    if not old_set or not new_set:
        return False
    return bool(new_set - old_set)


def _normalize_number(value) -> str:
    return str(value).strip().lower().replace(" ", "")
