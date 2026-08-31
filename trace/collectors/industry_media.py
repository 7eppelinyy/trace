"""产业媒体 Collector（P1）：TrendForce / DIGITIMES / EE Times / 集微网 / 芯智讯。

合规：license_mode=unknown 的来源只做
    事实重述 + AI摘要 + 来源名称 + 原文链接
不得重新发布完整正文。

失败必须抛出具体 SourceError，不得吞异常返回空数组伪装"无新数据"。
当前所有来源在 seed_sources.yaml 中默认禁用（选择器未验证），
启用后由本采集器负责工程化采集。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from trace.collectors.base import BaseCollector
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import ParseError, SourceError
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

_MEDIA_PAGES = [
    # TrendForce / DIGITIMES / EE Times 已改由 RSSCollector 走官方公开 feed 采集
    # （见 trace/data/feeds.yaml：官方 RSS = 明示授权，license_mode=public）。
    # RSS 路线优于此处的 HTML 列表页抓取，原因：
    #   1. 有真实 published_at（本采集器只能填 now()，会污染事件时间线）
    #   2. 支持 ETag / Last-Modified 条件请求，不重复下载
    #   3. 统一走 keyword_filter + bootstrap_days，成本可控
    # 因此这里必须移除它们，否则同一来源会被两个采集器重复采集。
    #
    # 以下两家 2026-08-31 实测无可用官方 feed（集微网 0 条/超时、
    # 芯智讯 服务端断开/超时），且站点条款未明确授权程序化抓取，
    # 按任务书 §6 保持 seed_sources.yaml 中 enabled=false，
    # 仅作 disabled_candidate 保留选择器，不进入生产链路。
    {
        "source_id": "src_jw_insights",
        "url": "https://www.jwei.com/news",
        "container": ".news-item, article",
        "language": "zh",
        "keywords": ["存储", "NAND", "DRAM", "HBM", "半导体", "芯片"],
    },
    {
        "source_id": "src_chipwise",
        "url": "https://www.icsmart.cn/news/",
        "container": ".news-list li, article",
        "language": "zh",
        "keywords": ["存储", "NAND", "DRAM", "HBM", "半导体"],
    },
]


class IndustryMediaCollector(BaseCollector):
    collector_type = "industry_media"

    @property
    def handled_source_ids(self) -> set[str]:
        return {p["source_id"] for p in _MEDIA_PAGES}

    def collect(self) -> list[RawItem]:
        enabled = {s.source_id for s in self.enabled_sources()}
        items: list[RawItem] = []
        errors: list[str] = []
        for page in _MEDIA_PAGES:
            if page["source_id"] not in enabled:
                continue
            try:
                items.extend(self._collect_page(page))
            except SourceError as exc:
                # 单源失败：记录健康状态，继续其他源；全部失败才向上暴露
                errors.append(f"{page['source_id']}: {exc}")
                self.health_repo.mark_failure(page["source_id"], str(exc), exc.category)
            else:
                self.health_repo.mark_success(page["source_id"], has_items=True)
        if errors and not items:
            raise ParseError("; ".join(errors))
        return items

    def _collect_page(self, page: dict) -> list[RawItem]:
        http = self.http_client(page["source_id"], rate_limit_qps=0.5)
        try:
            resp = http.get(page["url"])
            soup = BeautifulSoup(resp.text, "lxml")
        finally:
            http.close()

        items: list[RawItem] = []
        base_url = page["url"].rsplit("/", 1)[0]
        for node in soup.select(page["container"])[:50]:
            a = node if node.name == "a" else node.find("a")
            if a is None:
                continue
            title = (a.get_text() or "").strip()
            href = a.get("href") or ""
            if len(title) < 6 or not href:
                continue
            if page.get("keywords"):
                low = title.lower()
                if not any(k.lower() in low for k in page["keywords"]):
                    continue
            url = href if href.startswith("http") else base_url + (
                href if href.startswith("/") else "/" + href)
            items.append(RawItem(
                raw_item_id=raw_item_id(),
                source_id=page["source_id"],
                source_item_id=url,
                title=title,
                url=url,
                canonical_url=canonical_url(url),
                published_at=datetime.now(timezone.utc),
                fetched_at=datetime.now(timezone.utc),
                language=page["language"],
                content=None,
                reference=None,
                title_hash=title_hash(title),
                content_hash=content_hash(""),
            ))
        return items
