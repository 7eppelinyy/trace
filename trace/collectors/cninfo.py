"""巨潮资讯（cninfo）Collector（A股 P0，真实）。

官方公告查询接口：
    POST http://www.cninfo.com.cn/new/hisAnnouncement/query
    参数 stock = "{6位代码},{orgId}"，column = sse/szse（按市场）

orgId 通过官方 topSearch 接口解析并缓存（存于 collector_cursor），
同一证券不重复解析。

工程要求：
    timeout / retry / backoff / rate limit / cursor（增量） /
    structured logging / source-specific error。
失败必须抛出具体 SourceError，不得吞异常返回空数组伪装"无新数据"。

路线B说明（任务书 §5.2）：
    巨潮是沪深两市的统一法定信息披露平台（证监会指定）。
    上交所/深交所 Collector 定义为"巨潮公告的市场过滤适配器"
    （SSECollector/SZSECollector 继承本类，仅过滤各自市场），
    数据权威链：上市公司 → 巨潮（法定披露）→ 本采集器。
    默认只启用 src_cninfo 一个入口，避免三个名字不同、
    实际重复抓取同一数据且无法去重的 Collector。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trace.collectors.base import BaseCollector
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import (
    HttpClient,
    ParseError,
    SourceError,
    SourceStructureError,
    SourceUnavailableError,
)
from trace.common.ids import raw_item_id
from trace.db.repositories import SecurityRepo, WatchlistRepo
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

CNINFO_TOPSEARCH_API = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_QUERY_API = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE = "https://static.cninfo.com.cn/"

# 巨潮站点需要浏览器风格 UA + XHR 标记，否则可能被拒绝
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TraceEventRadar/0.2",
    "X-Requested-With": "XMLHttpRequest",
}

# 沪市/深市 → column 参数
_MARKET_COLUMN = {"SSE": "sse", "SZSE": "szse"}


class CNINFOCollector(BaseCollector):
    """巨潮公告采集器。

    路线B（任务书 §5.2）：本类同时是 SSE/SZSE 采集器的基类。
    子类通过 ``market_filter`` 只采集单一市场，``cursor_key`` 与
    ``handled_source_ids`` 各自独立，保证增量游标与健康状态分离。
    """

    collector_type = "cninfo"
    # 子类可覆盖：None = 沪深两市（主入口）
    market_filter: frozenset[str] | None = None

    @property
    def cursor_key(self) -> str:
        return "src_cninfo"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_cninfo"}

    # ------------------------------------------------------------------
    def collect(self) -> list[RawItem]:
        if not self.enabled_sources():
            raise SourceUnavailableError(
                f"{self.cursor_key}: no enabled cninfo market source")
        http = self.http_client("src_cninfo", rate_limit_qps=2.0,
                                headers=dict(_BROWSER_HEADERS))
        try:
            cursor = self.cursor_repo.get(self.cursor_key)
            # 插入序 dict：裁剪保留最新（sorted 字典序会把新条目裁掉）
            seen_ids: dict[str, None] = dict.fromkeys(cursor.get("seen_announcement_ids", []))
            org_cache: dict = dict(cursor.get("org_cache", {}))

            repo = SecurityRepo(self.db)
            targets = self._targets(repo)
            if not targets:
                raise ParseError("no CN watchlist securities to collect")

            items: list[RawItem] = []
            errors: list[str] = []
            for sec in targets:
                code = sec.ticker.split(".")[0]
                try:
                    org_id = self._resolve_org_id(http, code, org_cache)
                    if not org_id:
                        logger.warning("cninfo: no orgId for %s, skip", code)
                        continue
                    items.extend(self._collect_one(
                        http, code, org_id, _MARKET_COLUMN[sec.exchange], seen_ids))
                except SourceError as exc:
                    # 单只证券失败不中断其余证券；全部失败时整体暴露
                    errors.append(f"{code}: {exc}")
                    logger.warning("cninfo collect %s failed: %s", code, exc)

            if errors and not items:
                raise ParseError("all cninfo targets failed: " + "; ".join(errors[:5]))

            self.cursor_repo.set(self.cursor_key, {
                "seen_announcement_ids": list(seen_ids)[-3000:],
                "org_cache": org_cache,
            })
            return items
        finally:
            http.close()

    # ------------------------------------------------------------------
    @property
    def emit_source_id(self) -> str:
        """RawItem 归属的来源 ID（适配器子类使用各自的 source）。"""
        return sorted(self.handled_source_ids)[0]

    def _targets(self, repo: SecurityRepo):
        """按 Watchlist 过滤：默认 Watchlist 证券 + 用户通过 /watch 加入的证券。

        不对全市场扫描（任务书范围限制），避免首轮全量请求过大。
        适配器子类通过 market_filter 只保留单一交易所的证券。
        """
        watched_ids: set[str] = set()
        for sec in repo.list_watchlist_defaults():
            watched_ids.add(sec.security_id)
        watch_repo = WatchlistRepo(self.db)
        for row in self.db.query("SELECT DISTINCT security_id FROM watchlist"):
            watched_ids.add(row["security_id"])
        out = []
        for s in repo.list_all():
            if s.security_id not in watched_ids:
                continue
            if s.market != "CN" or s.exchange not in _MARKET_COLUMN:
                continue
            if self.market_filter is not None and s.exchange not in self.market_filter:
                continue
            out.append(s)
        return out

    # ------------------------------------------------------------------
    def _resolve_org_id(self, http: HttpClient, code: str,
                        org_cache: dict) -> str | None:
        if code in org_cache:
            return org_cache[code]
        resp = http.post(CNINFO_TOPSEARCH_API,
                         data={"keyWord": code, "maxNum": "10"})
        try:
            data = resp.json()
        except ValueError as exc:
            raise ParseError(f"cninfo topSearch JSON failed for {code}: {exc}") from exc
        if not isinstance(data, list):
            raise SourceStructureError(f"cninfo topSearch unexpected shape for {code}")
        for row in data:
            if str(row.get("code")) == code and row.get("orgId"):
                org_cache[code] = row["orgId"]
                return row["orgId"]
        return None

    # ------------------------------------------------------------------
    def _collect_one(self, http: HttpClient, code: str, org_id: str,
                     column: str, seen_ids: set[str]) -> list[RawItem]:
        resp = http.post(
            CNINFO_QUERY_API,
            data={
                "pageNum": "1", "pageSize": "30", "column": column,
                "tabName": "fulltext", "plate": "",
                "stock": f"{code},{org_id}",
                "searchkey": "", "secid": "", "category": "", "trade": "",
                "seDate": "", "sortName": "", "sortType": "",
                "isHLtitle": "true",
            },
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ParseError(f"cninfo query JSON failed for {code}: {exc}") from exc
        if not isinstance(data, dict):
            raise SourceStructureError(f"cninfo query unexpected shape for {code}")
        announcements = data.get("announcements")
        if announcements is None:
            # 接口结构变化或该股无公告：None 视为结构问题，空列表视为无新数据
            raise SourceStructureError(f"cninfo query missing 'announcements' for {code}")

        items: list[RawItem] = []
        for ann in announcements:
            ann_id = str(ann.get("announcementId") or "")
            title = (ann.get("announcementTitle") or "").strip()
            title = title.replace("<em>", "").replace("</em>", "")
            adj_url = ann.get("adjunctUrl") or ""
            if not ann_id or not title:
                continue
            if ann_id in seen_ids:
                continue            # 增量：不重复处理已采集公告
            seen_ids[ann_id] = None

            url = CNINFO_STATIC_BASE + adj_url if adj_url else ""
            published = None
            ts = ann.get("announcementTime")
            if isinstance(ts, (int, float)):
                published = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

            sec_name = ann.get("secName") or ""
            ann_type = ann.get("announcementType") or ""
            source_id = self.emit_source_id
            reference = (f"巨潮资讯公告 证券={code} {sec_name} "
                         f"类别={ann_type} 公告ID={ann_id} 登记来源={source_id}")
            items.append(RawItem(
                raw_item_id=raw_item_id(),
                source_id=source_id,
                source_item_id=f"{source_id}:{ann_id}",
                title=title,
                url=url,
                canonical_url=canonical_url(url),
                published_at=published,
                fetched_at=datetime.now(timezone.utc),
                language="zh",
                content=None,
                reference=reference,
                title_hash=title_hash(title),
                content_hash=content_hash(reference),
            ))
        return items
