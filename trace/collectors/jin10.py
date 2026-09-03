"""金十数据 Collector（MCP 智能开放平台，官方授权 API）。

合规：金十官方 MCP 智能开放平台（官方 Bearer token）授予在 AI 应用中使用其
快讯/日历/行情数据的授权，故 license_mode=licensed_ai_analysis（见
trace/data/seed_sources.yaml 的 src_jin10）。仅用于内部 AI 分析，不对外转卖/
重新分发。

数据流：金十 7×24 快讯 → RawItem → Level 1 确定性去重 → Stage A(LLM 抽取)
→ 语义聚类 → 评分 → Alert。快讯量大，必须过 keyword_filter（jin10 词表，
见 source_filters.yaml）控 LLM 成本；未命中词表的条目只入游标去重，不进 LLM。

工程要求：失败必须抛出具体 SourceError（AuthError / SourceUnavailable /
ParseError），不得吞异常伪装"无新数据"。token 从环境变量 JIN10_BEARER_TOKEN
读取（由 config 加载 .env 注入，见 config.py）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from trace.collectors.base import BaseCollector
from trace.collectors.filters import matches_keywords
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import (
    AuthError,
    ParseError,
    SourceError,
    SourceUnavailableError,
)
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

_SERVER_URL = "https://mcp.jin10.com/mcp"

# 每次采集最多翻页数（每页 20 条）与最大条目数；长驻循环每 interval 拉一轮，
# 首次/缺口用翻页+游标弥补，常规只补最新页，避免把千条快讯全量灌进 LLM。
_DEFAULT_MAX_PAGES = 3
_DEFAULT_MAX_ITEMS = 60
_DEFAULT_BOOTSTRAP_DAYS = 7


def _parse_time(s: str | None) -> datetime | None:
    """解析金十时间（如 '2026-09-03T21:11:59+08:00'）为 UTC datetime。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class Jin10Collector(BaseCollector):
    collector_type = "jin10"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_jin10"}

    # ------------------------------------------------------------------
    def _token(self) -> str:
        return os.environ.get("JIN10_BEARER_TOKEN", "").strip()

    def collect(self) -> list[RawItem]:
        token = self._token()
        if not token:
            raise AuthError("JIN10_BEARER_TOKEN 未配置，无法接入金十 MCP")

        source_id = "src_jin10"
        cursor = self.cursor_repo.get(source_id) or {}
        seen: dict[str, None] = dict.fromkeys(cursor.get("seen_ids", []))
        next_cursor = cursor.get("next_cursor")

        cfg = self.config.get("collectors.jin10", {}) or {}
        max_pages = int(cfg.get("max_pages", _DEFAULT_MAX_PAGES))
        max_items = int(cfg.get("max_items", _DEFAULT_MAX_ITEMS))
        keyword_filter = cfg.get("keyword_filter", "")
        bootstrap_days = float(cfg.get("bootstrap_days", _DEFAULT_BOOTSTRAP_DAYS))
        cutoff = datetime.now(timezone.utc) - timedelta(days=bootstrap_days)
        first_run = not bool(cursor.get("seen_ids"))

        try:
            raw_items, next_cursor_out = asyncio.run(
                self._fetch_flash(token, next_cursor, max_pages))
        except SourceError:
            raise
        except Exception as exc:  # mcp/httpx 等未分类异常 → 来源不可用
            raise SourceUnavailableError(
                f"jin10 flash fetch: {type(exc).__name__}: {exc}")

        items: list[RawItem] = []
        dropped_old = dropped_filter = dropped_dup = 0
        for fl in raw_items:
            content = (fl.get("content") or "").strip()
            url = (fl.get("url") or "").strip()
            if not content:
                continue
            entry_id = fl.get("url") or fl.get("time") or content
            # 首次接入的 bootstrap 窗口：更早的快讯只入游标，不下发
            published = _parse_time(fl.get("time"))
            if first_run and published is not None and published < cutoff:
                seen[entry_id] = None
                dropped_old += 1
                continue
            # Level 1 关键词初筛（控 LLM 成本；未命中只入游标）
            if keyword_filter and not matches_keywords(content, None, keyword_filter):
                seen[entry_id] = None
                dropped_filter += 1
                continue
            if entry_id in seen:
                dropped_dup += 1
                continue
            seen[entry_id] = None

            title = content[:120]
            items.append(RawItem(
                raw_item_id=raw_item_id(),
                source_id=source_id,
                source_item_id=entry_id,
                title=title,
                url=url,
                canonical_url=canonical_url(url) if url else title_hash(title),
                published_at=published,
                fetched_at=datetime.now(timezone.utc),
                language="zh",
                content=content,
                reference=None,
                title_hash=title_hash(title),
                content_hash=content_hash(content),
            ))
            if len(items) >= max_items:
                break

        next_cursor = dict(cursor)
        next_cursor["seen_ids"] = list(seen)[-2000:]
        if next_cursor_out:
            next_cursor["next_cursor"] = next_cursor_out
        self.cursor_repo.set(source_id, next_cursor)

        if dropped_old or dropped_filter or dropped_dup:
            logger.info(
                "[jin10] bootstrap_dropped=%d keyword_dropped=%d dup_dropped=%d "
                "returned=%d", dropped_old, dropped_filter, dropped_dup, len(items))
        self.last_keyword_filtered += dropped_filter
        return items

    # ------------------------------------------------------------------
    async def _fetch_flash(self, token: str, cursor: str | None,
                           max_pages: int) -> tuple[list[dict], str | None]:
        """通过官方 MCP client 拉取快讯流（游标分页）。"""
        # 惰性导入：mcp SDK 只在真正抓取时加载，模块级导入不依赖它
        from mcp import ClientSession
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        http = create_mcp_http_client(
            headers={"Authorization": f"Bearer {token}"})
        collected: list[dict] = []
        try:
            async with streamable_http_client(
                    _SERVER_URL, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    cur = cursor
                    for _ in range(max(1, max_pages)):
                        args = {"cursor": cur} if cur else {}
                        try:
                            res = await session.call_tool("list_flash", args)
                        except Exception as exc:
                            self._raise_tool_error("list_flash", exc)
                        text = self._text_of(res)
                        obj = json.loads(text)
                        if obj.get("status") not in (200, None):
                            msg = obj.get("message", "")
                            raise SourceUnavailableError(f"jin10 list_flash status != 200: {msg}")
                        data = obj.get("data") or {}
                        items = data.get("items") or []
                        collected.extend(items)
                        cur = data.get("next_cursor")
                        if not data.get("has_more") or not cur:
                            break
        finally:
            await http.aclose()
        return collected, cur

    def _text_of(self, res) -> str:
        for c in getattr(res, "content", None) or []:
            t = getattr(c, "text", None)
            if t:
                return t
        return str(res)

    def _raise_tool_error(self, tool: str, exc: Exception):
        msg = str(exc)
        low = msg.lower()
        if "401" in msg or "unauthorized" in low or "invalid token" in low \
                or "forbidden" in low:
            raise AuthError(f"jin10 {tool}: {msg}")
        raise SourceUnavailableError(f"jin10 {tool}: {type(exc).__name__}: {msg}")


def parse_flash_json(text: str) -> list[dict]:
    """供测试/离线验证用：解析 list_flash 响应文本。"""
    obj = json.loads(text)
    return (obj.get("data") or {}).get("items") or []
