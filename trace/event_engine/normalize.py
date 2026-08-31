"""RawItem 规范化：补齐 hash、canonical_url 等字段，供去重链路使用。"""

from __future__ import annotations

from datetime import datetime, timezone

from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.domain.models import RawItem


def normalize_raw_item(item: RawItem) -> RawItem:
    item.canonical_url = canonical_url(item.url)
    item.title_hash = title_hash(item.title)
    item.content_hash = content_hash(item.content or "")
    if item.fetched_at is None:
        item.fetched_at = datetime.now(timezone.utc)
    return item


def detect_language(text: str) -> str:
    """轻量语言检测：按 CJK 字符占比判断。"""
    if not text:
        return ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ratio = cjk / max(1, len(text))
    return "zh" if ratio > 0.15 else "en"
