"""内容哈希与规范化哈希（Level 1 确定性去重依赖）。"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 常见跟踪参数，规范化 URL 时移除
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "spm", "from",
}

_WS_RE = re.compile(r"\s+")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """去掉跟踪参数、统一小写域名、去掉末尾斜杠。"""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((
        parts.scheme.lower() or "https",
        parts.netloc.lower(),
        parts.path.rstrip("/") or "/",
        urlencode(query_pairs),
        "",
    ))


def normalize_title(title: str) -> str:
    """标题规范化：小写、压缩空白、去掉常见前缀标记。"""
    t = (title or "").strip().lower()
    t = _WS_RE.sub(" ", t)
    # 去掉形如 [Reuters] / 【路透社】 的前缀，但保留 "Company: ..." 实体前缀
    t = re.sub(r"^[\[【][^\]】]{1,20}[\]】]\s*[:：]?\s*", "", t)
    t = re.sub(r"^(breaking|update\s*\d*|flash|alert|exclusive|突发|快讯|最新|独家|早报|晚报)\s*[:：]?\s*", "", t)
    return t.strip()


def title_hash(title: str) -> str:
    return sha256_hex(normalize_title(title))


def content_hash(content: str) -> str:
    """空内容返回空字符串：避免所有无正文条目共享同一哈希而互相误判重复。"""
    c = _WS_RE.sub(" ", (content or "").strip().lower())
    if not c:
        return ""
    return sha256_hex(c)
