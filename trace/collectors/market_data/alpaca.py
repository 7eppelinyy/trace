"""Alpaca 行情 Provider（美股首版）。

无 API Key 时自动降级为 MockUSMarketProvider，保证开发环境可运行。
未来可替换为 Massive 或其他授权 Provider，业务代码不变。
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timezone

import httpx

from trace.collectors.market_data.base import (
    MarketDataProvider,
    Quote,
    UnavailableMarketProvider,
)
from trace.common.modes import TraceMode

logger = logging.getLogger(__name__)


class AlpacaProvider(MarketDataProvider):
    market = "US"

    def __init__(self):
        self.base_url = os.environ.get("ALPACA_BASE_URL", "https://data.alpaca.markets")
        self._http = httpx.Client(
            timeout=15,
            headers={
                "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
                "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
            },
        )

    def get_quote(self, ticker: str) -> Quote | None:
        try:
            resp = self._http.get(
                f"{self.base_url}/v2/stocks/{ticker}/quotes/latest",
                params={"feed": "iex"})
            resp.raise_for_status()
            data = resp.json().get("quote", {})
            price = data.get("ap") or data.get("bp")
        except Exception as exc:
            logger.warning("alpaca quote %s failed: %s", ticker, exc)
            return None
        return Quote(ticker=ticker, ts=datetime.now(timezone.utc),
                     last_price=float(price) if price else None)


class MockUSMarketProvider(MarketDataProvider):
    """开发阶段 Mock：基于确定种子的随机行情（显式标记 data_mode=mock）。"""

    market = "US"
    data_mode = "mock"

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def get_quote(self, ticker: str) -> Quote | None:
        base = 50 + hash(ticker) % 400
        last = base * (1 + self._rng.uniform(-0.03, 0.03))
        prev = base
        return Quote(
            ticker=ticker, ts=datetime.now(timezone.utc),
            last_price=round(last, 2), prev_close=round(prev, 2),
            change_pct_15m=round((last - prev) / prev * 100, 2),
            volume=self._rng.randint(100_000, 5_000_000),
            volume_ratio=round(self._rng.uniform(0.5, 3.0), 2),
        )


def build_us_provider() -> MarketDataProvider:
    if os.environ.get("ALPACA_API_KEY"):
        return AlpacaProvider()
    # 生产模式禁止 Mock 行情：未真实接入时显式标记 unavailable
    if TraceMode.is_production():
        logger.info("ALPACA_API_KEY not set (production): market data unavailable "
                    "(mock forbidden in production)")
        return UnavailableMarketProvider("US")
    logger.info("ALPACA_API_KEY not set, using MockUSMarketProvider")
    return MockUSMarketProvider()
