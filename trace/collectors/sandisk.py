"""SanDisk 官方 Collector（真实，P0）。

背景（2026-08-26 实测）：
    - IR RSS（investor.sandisk.com/rss.xml）在大陆直连超时，依赖代理；
    - 官方主站 sitemap（https://www.sandisk.com/sitemap.xml）可直连，
      其中 /company/newsroom/press-releases/ 共 53 条官方新闻稿
      （Investor Day、财报、Kioxia 合作等），全部官方一手内容，
      不使用任何第三方转载替代。

策略：
    1. 抓取官方 sitemap（低频 0.5 QPS + ETag/Last-Modified 条件请求）；
    2. 只取 press-releases 条目（公司正式公告；blogs/events 不进入）；
    3. 首次接入只回填最近 30 天（任务书 §16），更早条目只入游标；
    4. 发布时间从 URL 前缀解析（2026-08-13-...），缺失时用
       sitemap lastmod 兜底；
    5. 解析失败必须抛 SourceError，不得返回空数组伪装无新数据。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from trace.collectors.base import BaseCollector, DEFAULT_USER_AGENT
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import HttpClient, ParseError
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

SOURCE_ID = "src_sndk_ir"
SITEMAP_URL = "https://www.sandisk.com/sitemap.xml"
BOOTSTRAP_DAYS = 30

# press-release URL 前缀日期：.../press-releases/2026/2026-08-13-xxx
_URL_DATE_RE = re.compile(r"/press-releases/\d{4}/(20\d{2})-(\d{2})-(\d{2})-")


class SanDiskCollector(BaseCollector):
    """SanDisk 官方新闻稿：基于官方主站 sitemap，直连可用。"""

    collector_type = "sandisk_ir"

    @property
    def handled_source_ids(self) -> set[str]:
        return {SOURCE_ID}

    # ------------------------------------------------------------------
    def collect(self) -> list[RawItem]:
        http = self.http_client(SOURCE_ID, rate_limit_qps=0.5,
                                headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            cursor = self.cursor_repo.get(SOURCE_ID)
            headers: dict[str, str] = {}
            if cursor.get("etag"):
                headers["If-None-Match"] = cursor["etag"]
            if cursor.get("last_modified"):
                headers["If-Modified-Since"] = cursor["last_modified"]

            resp = http.get(SITEMAP_URL, headers=headers or None)
            if resp.status_code == 304:
                return []

            entries = parse_sitemap_press_releases(resp.text)
            if not entries:
                raise ParseError("sandisk sitemap structure changed: no press-releases")

            next_cursor = dict(cursor)
            etag = (resp.headers or {}).get("ETag")
            last_modified = (resp.headers or {}).get("Last-Modified")
            if etag:
                next_cursor["etag"] = etag
            if last_modified:
                next_cursor["last_modified"] = last_modified

            # 插入序 dict：裁剪保留最新（sorted 字典序会把新条目裁掉）
            seen: dict[str, None] = dict.fromkeys(cursor.get("seen_urls", []))
            first_run = "seen_urls" not in cursor
            cutoff = datetime.now(timezone.utc) - timedelta(days=BOOTSTRAP_DAYS)

            items: list[RawItem] = []
            dropped_old = 0
            for title, url, published in entries:
                if first_run and published is not None and published < cutoff:
                    seen[url] = None
                    dropped_old += 1
                    continue
                if url in seen:
                    continue
                seen[url] = None
                items.append(RawItem(
                    raw_item_id=raw_item_id(),
                    source_id=SOURCE_ID,
                    source_item_id=url,
                    title=title,
                    url=url,
                    canonical_url=canonical_url(url),
                    published_at=published,
                    fetched_at=datetime.now(timezone.utc),
                    language="en",
                    content=None,
                    reference="SanDisk official newsroom",
                    title_hash=title_hash(title),
                    content_hash=content_hash(title),
                ))

            next_cursor["seen_urls"] = list(seen)[-2000:]
            self.cursor_repo.set(SOURCE_ID, next_cursor)
            if dropped_old:
                logger.info("[%s] bootstrap dropped=%d (bootstrap_days=%d)",
                            SOURCE_ID, dropped_old, BOOTSTRAP_DAYS)
            return items
        finally:
            http.close()


def parse_sitemap_press_releases(xml_text: str) -> list[tuple[str, str, datetime | None]]:
    """解析官方 sitemap → [(标题, press-release URL, 发布时间)]。

    只取 /company/newsroom/press-releases/ 条目（正式公告）；
    blogs / events / our-technology 不属于公司公告，不进入。
    """
    results: list[tuple[str, str, datetime | None]] = []
    for m in re.finditer(
            r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]*)</lastmod>)?", xml_text):
        url, lastmod = m.group(1).strip(), (m.group(2) or "").strip()
        if "/company/newsroom/press-releases/" not in url:
            continue

        published: datetime | None = None
        dm = _URL_DATE_RE.search(url)
        if dm:
            try:
                published = datetime(int(dm.group(1)), int(dm.group(2)),
                                     int(dm.group(3)), tzinfo=timezone.utc)
            except ValueError:
                published = None
        if published is None and lastmod:
            try:
                published = datetime.strptime(lastmod[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                published = None

        title = _title_from_url(url)
        if not title:
            continue
        results.append((title, url, published))
    results.sort(key=lambda r: r[2] or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
    return results


def _title_from_url(url: str) -> str:
    """从 press-release URL slug 生成标题（详情页未抓取前的占位标题；
    slug 由官方生成，含日期前缀与完整语义，如
    2026-08-13-sandisk-investor-day-2026 → SanDisk Investor Day 2026）。"""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"^20\d{2}-\d{2}-\d{2}-", "", slug)
    words = [w for w in slug.split("-") if w]
    if not words:
        return ""
    title = " ".join(w.capitalize() if not w.isupper() else w for w in words)
    return "SanDisk: " + title
