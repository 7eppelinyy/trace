"""通用 RSS/Atom Collector（工程化）。

feed 列表集中配置在 trace/data/feeds.yaml（可扩展，不硬编码）。
每个 feed 独立采集：单个 feed 失败只影响该 source 的健康状态，
不影响其他 feed；失败抛具体 SourceError，由 BaseCollector.run 记录。

Source Completion Gate 扩展（任务书 §10/§11/§16）：
    - 条件请求：ETag / Last-Modified 游标（不重复下载未变化内容）
    - bootstrap_days：新来源首次接入只回填最近 N 天历史（默认 30），
      更早的历史条目只入游标、不下发（后续由 bootstrap suppression 兜底）
    - keyword_filter：Level 1 关键词初筛（词表在 source_filters.yaml），
      未命中条目入游标但不进入 Event Engine / LLM
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import mktime

import feedparser
import yaml

from trace.collectors.base import BaseCollector, DEFAULT_USER_AGENT
from trace.collectors.filters import matches_keywords
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import HttpClient, ParseError, SourceError
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

FEEDS_PATH = Path(__file__).parent.parent / "data" / "feeds.yaml"

DEFAULT_BOOTSTRAP_DAYS = 30   # 任务书 §16：首次接入只回填最近 7-30 天


def load_feeds() -> list[dict]:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("feeds", [])


class RSSCollector(BaseCollector):
    collector_type = "rss"

    @property
    def handled_source_ids(self) -> set[str]:
        # 通用 RSS 管道负责：政策官方源（Federal Register / Fed /
        # Commerce GovDelivery）+ 已确认提供官方公开 feed 的产业媒体
        # （TrendForce / DIGITIMES / EE Times，见 feeds.yaml 的启用依据）。
        # IR 官方 RSS 归 IRCollector，Micron 归 MicronCollector（429 回退）。
        # 注意：这里只声明「本采集器能处理」，实际是否采集仍由
        # source.enabled 决定（collect() 里与 enabled_sources() 取交集）。
        return {
            "src_federal_register", "src_fed", "src_commerce",
            "src_trendforce", "src_digitimes", "src_eetimes",
        }

    def collect(self) -> list[RawItem]:
        items: list[RawItem] = []
        handled = {s.source_id for s in self.enabled_sources()}
        errors: list[str] = []
        for feed in load_feeds():
            if feed["source_id"] not in handled:
                continue
            try:
                items.extend(self._collect_feed(feed))
            except SourceError as exc:
                # 单 feed 失败：记录该来源健康状态，继续其他 feed
                errors.append(f"{feed['source_id']}: {exc}")
                self.health_repo.mark_failure(
                    feed["source_id"], str(exc), exc.category,
                    http_status=getattr(exc, "http_status", None))
            else:
                self.health_repo.mark_success(feed["source_id"], has_items=True)
        if errors and not items:
            # 全部失败才向上暴露（否则部分成功按成功返回）
            raise ParseError("; ".join(errors))
        return items

    # ------------------------------------------------------------------
    def _collect_feed(self, feed: dict) -> list[RawItem]:
        source_id = feed["source_id"]
        http = self.http_client(source_id, rate_limit_qps=0.5,
                                headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            # 条件请求游标：ETag / Last-Modified（适用哪个用哪个）
            cursor = self.cursor_repo.get(source_id)
            headers: dict[str, str] = {}
            if cursor.get("etag"):
                headers["If-None-Match"] = cursor["etag"]
            if cursor.get("last_modified"):
                headers["If-Modified-Since"] = cursor["last_modified"]

            try:
                resp = http.get(feed["url"], headers=headers or None)
            except SourceError:
                raise
            not_modified = resp.status_code == 304
            parsed = None
            if not not_modified:
                parsed = feedparser.parse(resp.content)
                if parsed.bozo and not parsed.entries:
                    raise ParseError(f"feed parse failed: {source_id}")

            # 游标更新：新的 ETag / Last-Modified + 已知条目 ID（防重复下发）
            next_cursor = dict(cursor)
            etag = (resp.headers or {}).get("ETag") if not not_modified else cursor.get("etag")
            last_modified = ((resp.headers or {}).get("Last-Modified")
                             if not not_modified else cursor.get("last_modified"))
            if etag:
                next_cursor["etag"] = etag
            if last_modified:
                next_cursor["last_modified"] = last_modified
            # 插入序 dict（而非 set/sorted）：裁剪时保留最新见过的条目，
            # 避免字典序截断把新条目裁掉造成重复下发
            seen: dict[str, None] = dict.fromkeys(cursor.get("seen_item_ids", []))
            first_run = not cursor.get("seen_item_ids")

            items: list[RawItem] = []
            dropped_old = 0
            dropped_filter = 0
            entries = [] if not_modified else parsed.entries[:30]
            bootstrap_days = int(feed.get("bootstrap_days", DEFAULT_BOOTSTRAP_DAYS))
            cutoff = datetime.now(timezone.utc) - timedelta(days=bootstrap_days)
            keyword_filter = feed.get("keyword_filter", "")

            for entry in entries:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                entry_id = entry.get("id") or link
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    t = entry.get(key)
                    if t:
                        published = datetime.fromtimestamp(mktime(t), tz=timezone.utc)
                        break

                # bootstrap 回填窗口：首次接入只保留最近 N 天
                if first_run and published is not None and published < cutoff:
                    seen[entry_id] = None
                    dropped_old += 1
                    continue

                # Level 1 关键词初筛（政府/产业源不得无差别进 LLM）
                if keyword_filter and not matches_keywords(
                        title, entry.get("summary"), keyword_filter):
                    seen[entry_id] = None
                    dropped_filter += 1
                    continue

                if entry_id in seen:
                    continue
                seen[entry_id] = None

                summary = entry.get("summary") or ""
                items.append(RawItem(
                    raw_item_id=raw_item_id(),
                    source_id=source_id,
                    source_item_id=entry_id,
                    title=title,
                    url=link,
                    canonical_url=canonical_url(link),
                    published_at=published,
                    fetched_at=datetime.now(timezone.utc),
                    language=feed.get("language", ""),
                    content=summary or None,
                    reference=None,
                    title_hash=title_hash(title),
                    content_hash=content_hash(summary),
                ))

            next_cursor["seen_item_ids"] = list(seen)[-2000:]
            self.cursor_repo.set(source_id, next_cursor)
            if dropped_old or dropped_filter:
                logger.info("[%s] bootstrap dropped=%d keyword_dropped=%d "
                            "(bootstrap_days=%d)", source_id, dropped_old,
                            dropped_filter, bootstrap_days)
            self.last_keyword_filtered += dropped_filter
            return items
        finally:
            http.close()
