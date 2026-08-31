"""A股行情 Provider。

优先使用 Choice / 正式授权行情服务。
开发阶段允许 Mock / 历史数据 / 合规开发数据。
接入正式服务时实现 CNMarketDataProvider 接口即可，业务代码不变。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from trace.collectors.market_data.base import (
    MarketDataProvider,
    Quote,
    UnavailableMarketProvider,
)
from trace.common.modes import TraceMode

logger = logging.getLogger(__name__)


class MockCNMarketProvider(MarketDataProvider):
    """开发阶段 Mock（显式标记 data_mode=mock）。正式接入时替换为 Choice Adapter。"""

    market = "CN"
    data_mode = "mock"

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)

    def get_quote(self, ticker: str) -> Quote | None:
        base = 10 + abs(hash(ticker)) % 200
        last = base * (1 + self._rng.uniform(-0.05, 0.05))
        return Quote(
            ticker=ticker, ts=datetime.now(timezone.utc),
            last_price=round(last, 2), prev_close=round(base, 2),
            change_pct_15m=round((last - base) / base * 100, 2),
            volume=self._rng.randint(500_000, 20_000_000),
            volume_ratio=round(self._rng.uniform(0.5, 4.0), 2),
        )


def build_cn_provider() -> MarketDataProvider:
    # A股行情尚未真实接入：生产模式禁止 Mock，显式标记 unavailable
    if TraceMode.is_production():
        logger.info("CN market data not connected (production): unavailable "
                    "(mock forbidden in production)")
        return UnavailableMarketProvider("CN")
    logger.info("using MockCNMarketProvider (Phase 1 dev mode)")
    return MockCNMarketProvider()
