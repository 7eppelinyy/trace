"""官方政策源 Collector：BIS / 工信部 / 商务部（P0）。

实现方式：抓取官方页面列表并解析条目。页面结构可能变更，
解析失败时抛出具体 SourceError，绝不吞异常返回空数组伪装"无新数据"。

当前真实入口（2026-08-26 逐源实测）：
    - BIS:      https://www.bis.doc.gov/index.php/policy-focus/press-releases
    - 工信部:   https://www.miit.gov.cn/          （首页要闻，li + /art/ 链接 + 日期）
    - 商务部:   https://www.mofcom.gov.cn/        （首页要闻，li + art_ 链接 + 日期）
    - 证监会/统计局：选择器未充分验证，默认 disabled，不进关键路径

任务书 §5/§10：中国政府源不得无差别抓取 —— 标题先过
Level 1 关键词初筛（词表见 trace/data/source_filters.yaml），
未命中条目直接丢弃，减少无效 RawItem 与 LLM 成本。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from trace.collectors.base import BaseCollector, DEFAULT_USER_AGENT
from trace.collectors.filters import matches_keywords
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import ParseError, SourceError
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")

_POLICY_PAGES = [
    {
        "source_id": "src_bis",
        "url": "https://www.bis.doc.gov/index.php/policy-focus/press-releases",
        "mode": "links",
        "selector": "a",
        # 只取官方 press-release 链接：页面同时渲染大量导航/页脚链接
        # （"Skip to main content"、"Email notifications" 等），
        # 不过滤会把导航文本当成事件（任务书 §5：不得无差别抓取）
        "link_pattern": "/press-release/",
        "base": "https://www.bis.doc.gov",
        "language": "en",
        "keyword_filter": "",          # BIS press releases 本身即政策内容
    },
    {
        # 2026-08-26 实测：首页要闻列表（li > a[href*='/art/'] + 日期）
        "source_id": "src_miit",
        "url": "https://www.miit.gov.cn/",
        "mode": "list_items",
        "link_pattern": "/art/",
        "base": "https://www.miit.gov.cn",
        "language": "zh",
        "keyword_filter": "cn_policy",
    },
    {
        # 2026-08-26 实测：首页要闻列表（li > a[href*='art_'] + 日期）
        "source_id": "src_mofcom",
        "url": "https://www.mofcom.gov.cn/",
        "mode": "list_items",
        "link_pattern": "art_",
        "base": "https://www.mofcom.gov.cn",
        "language": "zh",
        "keyword_filter": "cn_policy",
    },
    {
        "source_id": "src_csrc",
        "url": "https://www.csrc.gov.cn/csrc/c100028/common_list.shtml",
        "mode": "links",
        "selector": "a",
        "base": "https://www.csrc.gov.cn",
        "language": "zh",
        "keyword_filter": "cn_policy",
    },
    {
        "source_id": "src_stats_cn",
        "url": "https://www.stats.gov.cn/sj/zxfb/",
        "mode": "links",
        "selector": "a",
        "base": "https://www.stats.gov.cn",
        "language": "zh",
        "keyword_filter": "cn_policy",
    },
]

_MIN_TITLE_LEN = 8


class PolicyCollector(BaseCollector):
    collector_type = "policy"

    @property
    def handled_source_ids(self) -> set[str]:
        return {p["source_id"] for p in _POLICY_PAGES}

    def collect(self) -> list[RawItem]:
        enabled = {s.source_id for s in self.enabled_sources()}
        items: list[RawItem] = []
        errors: list[str] = []
        for page in _POLICY_PAGES:
            if page["source_id"] not in enabled:
                continue
            try:
                items.extend(self._collect_page(page))
            except SourceError as exc:
                # 单页失败：记录该来源健康状态，继续其他页面；
                # 不得吞异常伪装成"无新数据"
                errors.append(f"{page['source_id']}: {exc}")
                self.health_repo.mark_failure(
                    page["source_id"], str(exc), exc.category,
                    http_status=getattr(exc, "http_status", None))
            else:
                self.health_repo.mark_success(page["source_id"], has_items=True)
        if errors and not items:
            raise ParseError("; ".join(errors))
        return items

    # ------------------------------------------------------------------
    def _collect_page(self, page: dict) -> list[RawItem]:
        http = self.http_client(page["source_id"], rate_limit_qps=0.5,
                                headers={"User-Agent": DEFAULT_USER_AGENT,
                                         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        try:
            resp = http.get(page["url"])
            soup = BeautifulSoup(resp.text, "lxml")
        finally:
            http.close()

        if page["mode"] == "list_items":
            entries = self._parse_list_items(soup, page)
        else:
            entries = self._parse_links(soup, page)

        if not entries:
            raise ParseError(f"{page['source_id']} page structure changed: no entries")

        keyword_filter = page.get("keyword_filter", "")
        items: list[RawItem] = []
        seen_urls: set[str] = set()
        filtered = 0
        for title, url, published in entries:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            # Level 1 关键词初筛：政府源不得无差别产生 RawItem
            if keyword_filter and not matches_keywords(title, None, keyword_filter):
                filtered += 1
                continue
            items.append(RawItem(
                raw_item_id=raw_item_id(),
                source_id=page["source_id"],
                source_item_id=url,
                title=title,
                url=url,
                canonical_url=canonical_url(url),
                published_at=published,
                fetched_at=datetime.now(timezone.utc),
                language=page["language"],
                content=None,
                reference=None,
                title_hash=title_hash(title),
                content_hash=content_hash(""),
            ))
            if len(items) >= 30:
                break
        self.last_keyword_filtered += filtered
        return items

    # ------------------------------------------------------------------
    def _parse_list_items(self, soup, page: dict) -> list[tuple]:
        """首页要闻列表：li 内链接 + 日期（工信部/商务部实测结构）。"""
        entries: list[tuple] = []
        for li in soup.select("li"):
            a = li.select_one(f"a[href*='{page['link_pattern']}']")
            if not a:
                continue
            title = (a.get_text() or "").strip()
            href = a.get("href") or ""
            if len(title) < _MIN_TITLE_LEN or not href:
                continue
            url = href if href.startswith("http") else page["base"] + (
                href if href.startswith("/") else "/" + href)
            published = None
            m = _DATE_RE.search(li.get_text())
            if m:
                try:
                    published = datetime.strptime(
                        m.group().replace("/", "-").replace(".", "-"),
                        "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    published = None
            entries.append((title, url, published))
        return entries

    def _parse_links(self, soup, page: dict) -> list[tuple]:
        """通用链接列表（BIS press releases 等）。

        若配置了 link_pattern，则只保留 href 命中该模式的链接，
        过滤导航/页脚噪声。
        """
        entries: list[tuple] = []
        pattern = page.get("link_pattern")
        for a in soup.select(page["selector"]):
            title = (a.get_text() or "").strip()
            href = a.get("href") or ""
            if len(title) < _MIN_TITLE_LEN or not href or href.startswith("javascript"):
                continue
            if pattern and pattern not in href:
                continue
            url = href if href.startswith("http") else page["base"] + (
                href if href.startswith("/") else "/" + href)
            entries.append((title, url, None))
        return entries
