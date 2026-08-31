"""来源关键词初筛（Level 1 廉价过滤，任务书 §10）。

两级过滤：
    Level 1（本模块）：关键词匹配，零 LLM 成本
    Level 2：只有通过初筛的候选项才进入 DeepSeek Stage A

政府 / 产业来源禁止无差别进入 LLM，避免 API 成本线性暴增。
词表集中在 trace/data/source_filters.yaml，禁止散落硬编码。
"""

from __future__ import annotations

from pathlib import Path

import yaml

_FILTERS_PATH = Path(__file__).parent.parent / "data" / "source_filters.yaml"
_cache: dict[str, list[str]] | None = None


def load_filters(path: Path | None = None) -> dict[str, list[str]]:
    """加载关键词词表：{filter_name: [关键词...]}（带进程内缓存）。"""
    global _cache
    if path is None and _cache is not None:
        return _cache
    with open(path or _FILTERS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    filters = {name: [str(k).lower() for k in (kws or [])]
               for name, kws in (data.get("filters") or {}).items()}
    if path is None:
        _cache = filters
    return filters


def matches_keywords(title: str, content: str | None,
                     filter_name: str, path: Path | None = None) -> bool:
    """Level 1 初筛：标题或正文命中任一关键词即放行（大小写不敏感）。

    filter_name 不存在或词表为空时放行（默认不静默丢数据，
    过滤策略必须在词表中显式声明）。
    """
    if not filter_name:
        return True
    keywords = load_filters(path).get(filter_name)
    if not keywords:
        return True
    text = f"{title} {content or ''}".lower()
    return any(kw in text for kw in keywords)
