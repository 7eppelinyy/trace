"""Micron 官方 IR Collector（真实，P0）。

背景（2026-08-26 实测）：
    - 官方 RSS（investors.micron.com/rss.xml）被 Cloudflare 限流 429
    - 官方新闻室页面（www.micron.com/about/newsroom/press-releases）可访问，
      列表结构：div.cmp-teaser__content（h2 标题 + "Month D, YYYY" 日期 +
      "Read article" 链接 → investors.micron.com 官方新闻稿）

策略（任务书 §3.2：不得靠提高频率硬闯 429）：
    1. 低频（0.2 QPS）尝试官方 RSS；
    2. RSS 429/不可用 → 回退官方新闻室页面（同为官方一手内容，
       不使用任何第三方转载）；
    3. 429 必须在 source_health 中显式记录（RATE_LIMITED +
       last_http_status=429），不得显示为"无新数据"；
    4. 游标按官方新闻稿 URL 去重，首次接入只回填最近 30 天。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
from bs4 import BeautifulSoup

from trace.collectors.base import BaseCollector, DEFAULT_USER_AGENT
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import (
    HttpClient,
    ParseError,
    RateLimitedError,
    SourceError,
)
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

SOURCE_ID = "src_micron_ir"
RSS_URL = "https://investors.micron.com/rss.xml"
NEWSROOM_URL = "https://www.micron.com/about/newsroom/press-releases"
BOOTSTRAP_DAYS = 30

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\.?\s+(\d{1,2}),\s+(20\d{2})")


class MicronCollector(BaseCollector):
    """Micron 官方 IR：RSS 优先，429 时回退官方新闻室页面。"""

    collector_type = "micron_ir"
    # 429 记录必须保留在 source_health（RATE_LIMITED 显式可见），
    # 不允许被 run() 的统一成功标记覆盖
    manages_own_health = True

    @property
    def handled_source_ids(self) -> set[str]:
        return {SOURCE_ID}

    # ------------------------------------------------------------------
    def collect(self) -> list[RawItem]:
        """本采集器自行管理健康状态（429 必须显式可见）。"""
        # 1) 官方 RSS（低频 + 退避；429 不硬闯）
        rss_error: SourceError | None = None
        try:
            items = self._collect_rss()
            self.health_repo.mark_success(SOURCE_ID, has_items=bool(items),
                                          http_status=200)
            return items
        except SourceError as exc:
            rss_error = exc
            self.health_repo.mark_failure(
                SOURCE_ID, f"official RSS failed ({exc.category}): {exc}",
                exc.category, http_status=getattr(exc, "http_status", None))
            logger.warning("[%s] RSS unavailable (%s), fallback to newsroom page",
                           SOURCE_ID, exc.category)

        # 2) 官方新闻室页面回退（仍是官方一手内容）
        items = self._collect_newsroom()
        if rss_error is not None and rss_error.category == "rate_limited":
            # 回退成功后重新标记成功，但保留 429 失败记录与
            # last_http_status=429 —— RATE_LIMITED 状态必须显式可见
            self.health_repo.mark_success(SOURCE_ID, has_items=bool(items))
            self.health_repo.mark_failure(
                SOURCE_ID,
                f"official RSS 429 rate limited (newsroom fallback active): {rss_error}",
                "rate_limited", http_status=429)
        else:
            self.health_repo.mark_success(SOURCE_ID, has_items=bool(items))
        return items

    # ------------------------------------------------------------------
    def _collect_rss(self) -> list[RawItem]:
        http = self.http_client(SOURCE_ID, rate_limit_qps=0.2,
                                headers={"User-Agent": DEFAULT_USER_AGENT},
                                max_retries=1)
        try:
            cursor = self.cursor_repo.get(SOURCE_ID)
            headers = {}
            if cursor.get("rss_etag"):
                headers["If-None-Match"] = cursor["rss_etag"]
            resp = http.get(RSS_URL, headers=headers or None)
            if resp.status_code == 304:
                return []
            parsed = feedparser.parse(resp.content)
            if parsed.bozo and not parsed.entries:
                raise ParseError("micron RSS parse failed")

            next_cursor = dict(cursor)
            if resp.headers.get("ETag"):
                next_cursor["rss_etag"] = resp.headers["ETag"]
            # 插入序 dict：裁剪保留最新（sorted 字典序会把新条目裁掉）
            seen: dict[str, None] = dict.fromkeys(cursor.get("seen_urls", []))
            cutoff = datetime.now(timezone.utc) - timedelta(days=BOOTSTRAP_DAYS)
            first_run = "seen_urls" not in cursor

            items: list[RawItem] = []
            for entry in parsed.entries[:30]:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()
                if not title or not link:
                    continue
                published = None
                for key in ("published_parsed", "updated_parsed"):
                    t = entry.get(key)
                    if t:
                        published = datetime.fromtimestamp(mktime(t), tz=timezone.utc)
                        break
                if first_run and published is not None and published < cutoff:
                    seen[link] = None
                    continue
                if link in seen:
                    continue
                seen[link] = None
                summary = entry.get("summary") or ""
                items.append(self._make_item(title, link, published, summary))
            next_cursor["seen_urls"] = list(seen)[-2000:]
            self.cursor_repo.set(SOURCE_ID, next_cursor)
            return items
        finally:
            http.close()

    # ------------------------------------------------------------------
    def _collect_newsroom(self) -> list[RawItem]:
        http = self.http_client(SOURCE_ID, rate_limit_qps=0.2,
                                headers={"User-Agent": DEFAULT_USER_AGENT,
                                         "Accept": "text/html"},
                                max_retries=1)
        try:
            resp = http.get(NEWSROOM_URL)
            teasers = parse_newsroom_html(resp.text)
            if not teasers:
                raise ParseError(
                    "micron newsroom structure changed: no press-release teasers")

            cursor = self.cursor_repo.get(SOURCE_ID)
            seen: dict[str, None] = dict.fromkeys(cursor.get("seen_urls", []))
            cutoff = datetime.now(timezone.utc) - timedelta(days=BOOTSTRAP_DAYS)
            first_run = "seen_urls" not in cursor

            items: list[RawItem] = []
            for title, link, published in teasers:
                if first_run and published is not None and published < cutoff:
                    seen[link] = None
                    continue
                if link in seen:
                    continue
                seen[link] = None
                items.append(self._make_item(title, link, published, None))
            cursor["seen_urls"] = list(seen)[-2000:]
            self.cursor_repo.set(SOURCE_ID, cursor)
            return items
        finally:
            http.close()

    # ------------------------------------------------------------------
    def _make_item(self, title: str, url: str,
                   published: datetime | None, summary: str | None) -> RawItem:
        return RawItem(
            raw_item_id=raw_item_id(),
            source_id=SOURCE_ID,
            source_item_id=url,
            title=title,
            url=url,
            canonical_url=canonical_url(url),
            published_at=published,
            fetched_at=datetime.now(timezone.utc),
            language="en",
            content=summary or None,
            reference="Micron official newsroom",
            title_hash=title_hash(title),
            content_hash=content_hash(summary or title),
        )


def parse_newsroom_html(html: str) -> list[tuple[str, str, datetime | None]]:
    """解析官方新闻室列表页 → [(标题, 官方新闻稿 URL, 发布时间)]。

    结构（2026-08-26 实测）：每个新闻卡片是 div.cmp-teaser__content，
    内含 h2 标题、"August 24, 2026" 日期文本、"Read article" 链接。
    解析失败必须抛 SourceError，不得返回空数组伪装无新数据。
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str, datetime | None]] = []
    seen: set[str] = set()
    for teaser in soup.select("div.cmp-teaser__content"):
        h = teaser.find(["h1", "h2", "h3", "h4"])
        a = teaser.find("a", href=True)
        if not h or not a:
            continue
        title = h.get_text(strip=True)
        href = a["href"]
        # 只接受指向官方新闻稿的链接（investors.micron.com 或站内绝对路径）
        if not title or not href.startswith(
                ("https://investors.micron.com/", "http://investors.micron.com/")):
            continue
        published = None
        m = _DATE_RE.search(teaser.get_text(" ", strip=True))
        if m:
            month = _MONTHS.get(m.group(1))
            if month:
                try:
                    published = datetime(int(m.group(3)), month, int(m.group(2)),
                                         tzinfo=timezone.utc)
                except ValueError:
                    published = None
        if href in seen:
            continue
        seen.add(href)
        results.append((title, href, published))
    return results
