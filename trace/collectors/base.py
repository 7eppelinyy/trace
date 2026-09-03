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
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        # 上次运行的 monotonic 时间戳（仅长驻循环按间隔调度时使用）
        self._last_run_at: float | None = None

    # ------------------------------------------------------------------
    def seconds_until_due(self) -> float:
        """距下次允许采集的秒数（0 = 已到期）。

        interval_seconds <= 0 表示每轮都跑；首次运行视为已到期。
        """
        if self.interval_seconds <= 0 or self._last_run_at is None:
            return 0.0
        return max(0.0, self._last_run_at + self.interval_seconds - time.monotonic())

    def mark_ran(self) -> None:
        self._last_run_at = time.monotonic()

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
    """采集器注册表。

    - respect_intervals=True（长驻 run/bot 循环）：未到 interval_seconds 的
      采集器本轮跳过（不请求、不计入 source 结果），run-once 验收不受影响。
    - parallel_workers > 1 时并行执行到期的采集器（各采集器内部仍保持
      自己的 QPS 限速；Database 为 thread-local 连接，SQLite 侧由
      busy_timeout 兜底并发写）。
    """

    def __init__(self, parallel_workers: int = 1):
        self._collectors: list[BaseCollector] = []
        self._parallel_workers = max(1, int(parallel_workers))

    def register(self, collector: BaseCollector) -> None:
        self._collectors.append(collector)

    @property
    def collectors(self) -> list[BaseCollector]:
        return list(self._collectors)

    def run_all(self, *, respect_intervals: bool = False
                ) -> tuple[list[RawItem], list[CollectResult]]:
        due: list[BaseCollector] = []
        for c in self._collectors:
            if respect_intervals and c.seconds_until_due() > 0:
                logger.info("[scheduler] collector %s skipped (interval=%ss not due)",
                            c.collector_type, c.interval_seconds)
                continue
            due.append(c)

        results: list[CollectResult | None] = [None] * len(due)
        if self._parallel_workers > 1 and len(due) > 1:
            with ThreadPoolExecutor(
                    max_workers=min(self._parallel_workers, len(due))) as pool:
                futures = {pool.submit(c.run): i for i, c in enumerate(due)}
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception as exc:  # run() 内部已兜底；此处防御线程级异常
                        logger.exception("collector %s crashed", due[i].collector_type)
                        results[i] = CollectResult(
                            source_ids=sorted(due[i].handled_source_ids),
                            status="failed", error=str(exc)[:300],
                            error_category="structure_changed")
        else:
            for i, c in enumerate(due):
                results[i] = c.run()

        for c in due:
            c.mark_ran()
        items: list[RawItem] = []
        final: list[CollectResult] = []
        for res in results:
            if res is None:
                continue
            final.append(res)
            items.extend(res.items)
        return items, final


def build_default_registry(db: Database, config) -> CollectorRegistry:
    """按规划书 §7 注册所有采集器（真实链路）。

    采集间隔由 settings.yaml collectors.interval_seconds 控制
    （长驻循环生效，见 CollectorRegistry.run_all）；并行度由
    collectors.parallel_workers 控制（默认 4，设 1 退回串行）。
    """
    from trace.collectors.cninfo import CNINFOCollector
    from trace.collectors.industry_media import IndustryMediaCollector
    from trace.collectors.ir import IRCollector
    from trace.collectors.jin10 import Jin10Collector
    from trace.collectors.micron import MicronCollector
    from trace.collectors.policy import PolicyCollector
    from trace.collectors.rss import RSSCollector
    from trace.collectors.sandisk import SanDiskCollector
    from trace.collectors.sec import SECCollector
    from trace.collectors.sse import SSECollector
    from trace.collectors.szse import SZSECollector

    registry = CollectorRegistry(
        parallel_workers=int(config.get("collectors.parallel_workers", 4)))
    for cls in (SECCollector, IRCollector, SanDiskCollector, MicronCollector,
                CNINFOCollector, SSECollector, SZSECollector, PolicyCollector,
                RSSCollector, IndustryMediaCollector, Jin10Collector):
        registry.register(cls(db, config))
    return registry
