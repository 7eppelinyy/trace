"""Market Confirmation：把行情变化映射为 1–10 确认分。

Phase 1 行情主要用于 Market Confirmation，不做无新闻异动报警。
规则（集中在本模块，可调）：
    - 无行情数据：5.0（中性，不加减分）
    - 使用事件后 reaction_window 内（默认取 15m 变化）的方向与幅度
    - 方向与 impact direction 一致才加分，反向扣分
"""

from __future__ import annotations

from trace.collectors.market_data.base import MarketDataProvider, Quote


def confirmation_score(direction: str, change_pct: float | None) -> float:
    """direction: bullish/bearish/neutral/mixed/uncertain。返回 1–10。"""
    if change_pct is None:
        return 5.0
    if direction in ("neutral", "mixed", "uncertain"):
        return 5.0
    magnitude = min(abs(change_pct) / 5.0, 1.0) * 5.0  # ±5% 映射到 ±5 分
    if direction == "bullish":
        return max(1.0, min(10.0, 5.0 + magnitude))
    if direction == "bearish":
        return max(1.0, min(10.0, 5.0 - magnitude))
    return 5.0


class MarketConfirmer:
    def __init__(self, us_provider: MarketDataProvider, cn_provider: MarketDataProvider):
        self._providers = {us_provider.market: us_provider, cn_provider.market: cn_provider}

    def quote(self, market: str, ticker: str) -> Quote | None:
        provider = self._providers.get(market)
        if provider is None:
            return None
        return provider.get_quote(ticker)

    def score_for(self, market: str, ticker: str, direction: str) -> tuple[float, Quote | None]:
        q = self.quote(market, ticker)
        if q is None:
            return 5.0, None
        change = q.change_pct_15m if q.change_pct_15m is not None else q.change_pct_from_prev()
        return confirmation_score(direction, change), q

    def data_mode(self, market: str) -> str:
        """返回该市场的行情数据模式：real / mock / none。

        任务书 §7：使用 Mock 行情必须明确标记 market_data_mode=mock；
        没有行情标记 none，不得生成虚构市场确认。
        """
        provider = self._providers.get(market)
        if provider is None:
            return "none"
        return getattr(provider, "data_mode", "real")
