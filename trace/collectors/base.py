"""统一 Collector Interface（工程化版本）。

所有数据源必须实现 BaseCollector：
    Collector
    ├── SECCollector          （真实，SEC EDGAR）
    ├── IRCollector           （真实，公司官方 RSS/Newsroom）
    ├── SanDiskCollector      （真实，SanDisk 官方主站 sitemap）
    ├── MicronCollector       （真实，Micron RSS + 429 新闻室回退）
    ├── CNINFOCollector       （真实，巨潮公告，按 Watchlist 股票过滤）
    ├── SSECollector          （路线B：巨潮的市场过滤适配器，见文档）
    ├── SZSECollector         （路线B：巨潮的市场过滤适配器，见文档）
    ├── PolicyCollector       （真实，官方政策页面）
    ├── RSSCollector          （通用 RSS）
    └── IndustryMediaCollector（真实，产业媒体列表页）

工程要求：
    timeout / retry / backoff / rate limit / cursor / structured logging /
    source-specific error。

必须区分：网络错误 / 来源限流 / 来源结构变化 / 无新数据 /
解析失败 / 鉴权失败 / 来源暂时不可用。
不得将所有异常吞掉并返回空数组——失败必须写入 source_health 并向上暴露。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from trace.common.http_client import (
    AuthError,
    NetworkError,
    ParseError,
    RateLimitedError,
    SourceError,
    SourceUnavailableError,
    SourceStructureError,
)
from trace.db.connection import Database
from trace.db.health import CursorRepo, SourceHealthRepo
from trace.db.repositories import SourceRepo
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "TraceEventRadar/0.2 (research use; contact research@example.com)"


@dataclass
class CollectResult:
    """一次采集的结构化结果。"""
    source_ids: list[str]
    items: list[RawItem] = field(default_factory=list)
    status: str = "ok"                 # ok / failed / no_new_data
    error: str = ""
    error_category: str = ""
    # Level 1 关键词初筛被拦截的条目数（任务书 §10/§17 成本统计）
    keyword_filtered: int = 0


class BaseCollector(ABC):
    collector_type: str = "base"
    # 自管理健康状态的采集器（如 Micron：429 记录必须保留，
    # 不能被 run() 的统一 mark_success 覆盖）
    manages_own_health: bool = False

    def __init__(self, db: Database, config):
        self.db = db
        self.config = config
        self.source_repo = SourceRepo(db)
        self.health_repo = SourceHealthRepo(db)
        self.cursor_repo = CursorRepo(db)
        # Level 1 关键词初筛拦截计数（每次 collect 后由 run 归零）
        self.last_keyword_filtered = 0
        intervals = config.get("collectors.interval_seconds", {})
        self.interval_seconds = int(intervals.get(self.collector_type, 600))

    # ------------------------------------------------------------------
    @property
    @abstractmethod
    def handled_source_ids(self) -> set[str]:
        ...

    @abstractmethod
    def collect(self) -> list[RawItem]:
        """抓取一次。成功（含无新数据）返回列表；失败必须抛出具体 SourceError。"""
        ...

    def enabled_sources(self) -> list:
        """本采集器负责且已启用的来源（未授权来源永不运行）。"""
        return [
            s for s in self.source_repo.list_enabled()
            if s.source_id in self.handled_source_ids
        ]

    # ------------------------------------------------------------------
    def http_client(self, source_id: str, *, rate_limit_qps: float = 1.0,
                    headers: dict | None = None, max_retries: int = 3,
                    backoff_base: float = 1.5) -> "HttpClient":
        """创建带统一工程参数的 HttpClient（timeout 来自配置，失败分类上抛）。"""
        from trace.common.http_client import HttpClient
        return HttpClient(
            source_id,
            timeout=float(self.config.get("collectors.http.timeout_seconds", 30)),
            max_retries=max_retries,
            backoff_base=backoff_base,
            rate_limit_qps=rate_limit_qps,
            headers=headers or {"User-Agent": DEFAULT_USER_AGENT},
        )

    @property
    def sec_user_agent(self) -> str:
        """SEC 公平访问：User-Agent = "公司名 联系邮箱"（来自配置，禁止硬编码）。"""
        contact = getattr(self.config, "sec_contact", None)
        return contact.user_agent if contact else DEFAULT_USER_AGENT

    # ------------------------------------------------------------------
    def run(self) -> CollectResult:
        """执行采集并维护来源健康状态。

        - 网络错误/限流/不可用：记录失败，返回 failed（不吞异常伪装成无新数据）
        - 解析失败/结构变化：记录失败并返回 failed
        - 成功但无新数据：ok + no_new_data
        """
        source_ids = sorted(self.handled_source_ids)
        enabled = self.enabled_sources()
        if not enabled:
            return CollectResult(source_ids=source_ids, status="disabled",
                                 error="no enabled sources (license or config)")
        try:
            items = self.collect()
        except SourceError as exc:
            msg = str(exc)
            logger.error("[%s] collect failed (%s): %s",
                         self.collector_type, exc.category, msg)
            for sid in source_ids:
                self.health_repo.mark_failure(sid, msg, exc.category,
                                              http_status=getattr(exc, "http_status", None))
            return CollectResult(source_ids=source_ids, status="failed",
                                 error=msg, error_category=exc.category)
        except Exception as exc:  # 未预期错误也按结构变化暴露，不静默
            msg = f"unexpected {type(exc).__name__}: {exc}"
            logger.exception("[%s] collect failed unexpectedly", self.collector_type)
            for sid in source_ids:
                self.health_repo.mark_failure(sid, msg, "structure_changed")
            return CollectResult(source_ids=source_ids, status="failed",
                                 error=msg, error_category="structure_changed")

        status = "ok" if items else "no_new_data"
        keyword_filtered = self.last_keyword_filtered
        self.last_keyword_filtered = 0
        if self.manages_own_health:
            # 健康状态已在 collect() 内精细记录（含 429），不得统一覆盖
            return CollectResult(source_ids=source_ids, items=items, status=status,
                                 keyword_filtered=keyword_filtered)
        last_item_at = None
        dated = [i for i in items if i.published_at is not None]
        if dated:
            last_item_at = max(dated, key=lambda i: i.published_at).published_at.isoformat()
        for sid in source_ids:
            self.health_repo.mark_success(sid, has_items=bool(items),
                                          last_item_at=last_item_at)
        return CollectResult(source_ids=source_ids, items=items, status=status,
                             keyword_filtered=keyword_filtered)


class CollectorRegistry:
    def __init__(self):
        self._collectors: list[BaseCollector] = []

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)

    @property
    def collectors(self) -> list[BaseCollector]:
        return list(self._collectors)

    def run_all(self) -> tuple[list[RawItem], list[CollectResult]]:
        items: list[RawItem] = []
        results: list[CollectResult] = []
        for c in self._collectors:
            res = c.run()
            results.append(res)
            items.extend(res.items)
        return items, results


def build_default_registry(db: Database, config) -> CollectorRegistry:
    """按规划书 §7 注册所有采集器（真实链路）。"""
    from trace.collectors.cninfo import CNINFOCollector
    from trace.collectors.industry_media import IndustryMediaCollector
    from trace.collectors.ir import IRCollector
    from trace.collectors.micron import MicronCollector
    from trace.collectors.policy import PolicyCollector
    from trace.collectors.rss import RSSCollector
    from trace.collectors.sandisk import SanDiskCollector
    from trace.collectors.sec import SECCollector
    from trace.collectors.sse import SSECollector
    from trace.collectors.szse import SZSECollector

    registry = CollectorRegistry()
    for cls in (SECCollector, IRCollector, SanDiskCollector, MicronCollector,
                CNINFOCollector, SSECollector, SZSECollector, PolicyCollector,
                RSSCollector, IndustryMediaCollector):
        registry.register(cls(db, config))
    return registry
