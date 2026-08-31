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


class ExactDedup:
    def __init__(self, db: Database):
        self.raw_repo = RawItemRepo(db)

    def check(self, item: RawItem) -> DedupResult:
        if item.source_item_id and self.raw_repo.exists_same_source_item(
                item.source_id, item.source_item_id):
            return DedupResult(True, "source_item_id")
        if item.canonical_url and self.raw_repo.exists_canonical_url(item.canonical_url):
            return DedupResult(True, "canonical_url")
        if item.title_hash and self.raw_repo.exists_title_hash(item.title_hash):
            return DedupResult(True, "title_hash")
        if item.content_hash and self.raw_repo.exists_content_hash(item.content_hash):
            return DedupResult(True, "content_hash")
        return DedupResult(False)
