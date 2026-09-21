"""Level 1 确定性去重。

使用 source_item_id / canonical_url / normalized_title hash / normalized_content hash。
任何一条命中即判定为已存在，不进入后续流程。
"""

from __future__ import annotations

from dataclasses import dataclass

from trace.db.connection import Database
from trace.db.repositories import RawItemRepo
from trace.domain.models import RawItem


@dataclass
class DedupResult:
    is_duplicate: bool
    reason: str = ""
    is_revision: bool = False
    existing_raw_item_id: str | None = None
    existing_event_id: str | None = None


class ExactDedup:
    def __init__(self, db: Database):
        self.raw_repo = RawItemRepo(db)

    def check(self, item: RawItem) -> DedupResult:
        # 1. 相同 source_id + source_item_id：平台级唯一条目 ID 命中即判定为已存在
        if item.source_item_id:
            existing = self.raw_repo.find_by_source_item(item.source_id, item.source_item_id)
            if existing:
                same = existing.content_hash == item.content_hash and existing.title_hash == item.title_hash
                return DedupResult(same, "source_item_id" if same else "source_content_updated",
                                   is_revision=not same, existing_raw_item_id=existing.raw_item_id,
                                   existing_event_id=existing.event_id)

        # 2. 相同 canonical_url：若内容 hash 相同为完全重复；若内容变更则为文档修订更正（F11）
        if item.canonical_url:
            existing = self.raw_repo.find_by_canonical_url(item.canonical_url)
            if existing:
                if existing.content_hash and item.content_hash and existing.content_hash == item.content_hash:
                    return DedupResult(True, "canonical_url", existing_raw_item_id=existing.raw_item_id, existing_event_id=existing.event_id)
                if not item.content_hash and not existing.content_hash:
                    return DedupResult(True, "canonical_url", existing_raw_item_id=existing.raw_item_id, existing_event_id=existing.event_id)
                return DedupResult(
                    is_duplicate=False,
                    reason="url_content_updated",
                    is_revision=True,
                    existing_raw_item_id=existing.raw_item_id,
                    existing_event_id=existing.event_id,
                )

        # 3. 相同 title_hash：同源同标题判定为重复；跨源同标题为独立佐证，不予丢弃（F11）
        if item.title_hash:
            matches = self.raw_repo.find_by_title_hash(item.title_hash)
            if matches:
                same_source = [m for m in matches if m.source_id == item.source_id]
                exact = next((m for m in same_source if m.content_hash == item.content_hash), None)
                if exact:
                    return DedupResult(True, "title_hash", existing_raw_item_id=exact.raw_item_id, existing_event_id=exact.event_id)
                # 跨源同标题：作为新证据版本进入后续聚类，不作为重复拦截

        # 4. 相同 content_hash：同源判定为绝对重复
        if item.content_hash:
            existing = self.raw_repo.find_by_content_hash(item.content_hash)
            if existing and existing.source_id == item.source_id:
                return DedupResult(True, "content_hash", existing_raw_item_id=existing.raw_item_id, existing_event_id=existing.event_id)

        return DedupResult(False)
