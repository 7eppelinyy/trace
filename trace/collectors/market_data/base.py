"""行情数据 Provider（Market Confirmation）。

Phase 1 接行情，但行情主要用于 Market Confirmation，
而不是主动无新闻异动报警。

业务逻辑不绑定某一个行情供应商：
    USMarketDataProvider（首版 Alpaca，可换 Massive 或其他授权服务）
    CNMarketDataProvider（首版 Choice/授权服务，开发期允许 Mock）

至少提供：last price / previous close / 1m / 5m / 15m 涨跌、
volume / volume ratio / pre/post market（美股）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Quote:
    ticker: str
    ts: datetime                                  # 兼容别名（优先取 market_timestamp，兜底 fetched_at）
    last_price: float | None = None
    prev_close: float | None = None
    change_pct_day: float | None = None          # 当日涨跌幅（与 15m 严格分离，F15）
    change_pct_1m: float | None = None
    change_pct_5m: float | None = None
    change_pct_15m: float | None = None          # 严格 15 分钟区间变动
    volume: int | None = None
    volume_ratio: float | None = None
    session: str = "regular"                      # regular / pre / post
    market_timestamp: datetime | None = None     # 交易所真实成交/行情时间戳
    fetched_at: datetime | None = None           # 本系统发起抓取并解析完成的时刻
    currency: str = "USD"                         # USD / CNY
    source: str = ""                              # alpaca / tencent / mock / unavailable
    is_delayed: bool = False                      # 是否为延时行情（如美股公开快照通常延迟 15 分钟）
    change_basis: str = "prev_close"              # prev_close / open / previous_bar

    def change_pct_from_prev(self) -> float | None:
        if self.change_pct_day is not None:
            return self.change_pct_day
        if self.last_price is None or not self.prev_close:
            return None
        return round((self.last_price - self.prev_close) / self.prev_close * 100, 2)

    @property
    def quality(self) -> str:
        from trace.collectors.market_data.time_quality import quote_quality
        return quote_quality(self)


class MarketDataProvider:
    market: str = ""
    # 数据模式标记（任务书 §7）：real / mock / unavailable。
    # Mock Provider 必须显式标记，Telegram 消息与 event_impact
    # 不得把 Mock 行情包装成真实市场确认。
    data_mode: str = "real"

    def get_quote(self, ticker: str) -> Quote | None:
        raise NotImplementedError

    def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for t in tickers:
            q = self.get_quote(t)
            if q:
                out[t] = q
        return out


class UnavailableMarketProvider(MarketDataProvider):
    """生产模式行情未真实接入占位：禁止 Mock。

    - 不提供任何行情数据（get_quote 恒为 None）
    - 显式标记 data_mode="unavailable"（不进入评分、不在消息中伪装行情）
    - 市场确认分保持中性 5.0（由 MarketConfirmer 对 None 行情保证）
    """

    data_mode = "unavailable"

    def __init__(self, market: str):
        self.market = market

    def get_quote(self, ticker: str) -> Quote | None:
        return None
