"""行情 Provider 离线测试。

任务书 §7/§16：真实行情必须产出可用于 Market Confirmation 的字段
（prev_close / change_pct_15m）；缺字段保持 None 退回中性分，
不得用 Mock 数据补位。
"""

from __future__ import annotations
from datetime import datetime, timezone

import pytest

from trace.collectors.market_data.alpaca import AlpacaProvider


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAlpacaHTTP:
    """按 URL 前缀返回预置响应的假 httpx.Client。"""

    def __init__(self, snapshot: dict, bars: list | Exception | None):
        self.snapshot = snapshot
        self.bars = bars
        self.calls: list[str] = []

    def get(self, url, params=None):
        self.calls.append(url)
        if url.endswith("/snapshot"):
            return _FakeResp(self.snapshot)
        if url.endswith("/bars"):
            if isinstance(self.bars, Exception):
                raise self.bars
            return _FakeResp({"bars": self.bars or []})
        raise AssertionError(f"unexpected url {url}")


def _provider(snapshot: dict, bars) -> AlpacaProvider:
    provider = AlpacaProvider()
    provider._http = _FakeAlpacaHTTP(snapshot, bars)
    return provider


_SNAP = {
    "latestTrade": {"p": 101.5, "t": datetime.now(timezone.utc).isoformat()},
    "dailyBar": {"c": 101.0},
    "prevDailyBar": {"c": 100.0},
}


def test_alpaca_quote_has_confirmation_fields():
    """真实 quote：prev_close 来自 prevDailyBar，15m 变动来自 15Min bars。"""
    provider = _provider(_SNAP, [{"o": 100.0, "c": 101.0, "t": "2026-09-18T19:30:00Z"},
                                 {"o": 101.0, "c": 102.0, "t": "2026-09-18T19:45:00Z"}])
    q = provider.get_quote("MU")
    assert q.last_price == 101.5
    assert q.prev_close == 100.0
    # 首根开盘 100.0 → 末根收盘 102.0
    assert q.change_pct_15m == pytest.approx(0.99)
    # 市场确认不再恒为中性：15m 变动可产生非 5 分
    from trace.collectors.market_data.cn import MockCNMarketProvider
    from trace.collectors.market_data.confirmation import MarketConfirmer
    confirmer = MarketConfirmer(provider, MockCNMarketProvider())
    c = confirmer.confirm("US", "MU", "bullish")
    assert c.score > 5.0 and c.quote is not None


def test_alpaca_quote_tolerates_missing_bars():
    """bars 缺失/失败：quote 仍返回（prev_close 可用），15m 保持 None。"""
    provider = _provider(_SNAP, RuntimeError("bars unavailable"))
    q = provider.get_quote("MU")
    assert q.prev_close == 100.0
    assert q.change_pct_15m is None

    provider2 = _provider(_SNAP, [])
    q2 = provider2.get_quote("MU")
    assert q2.change_pct_15m is None


def test_alpaca_snapshot_failure_returns_none():
    """snapshot 整体失败：返回 None（MarketConfirmer 退回中性 5 分）。"""
    provider = AlpacaProvider()

    class _Boom:
        def get(self, url, params=None):
            raise RuntimeError("down")

    provider._http = _Boom()
    assert provider.get_quote("MU") is None


def test_market_confirmer_neutral_when_no_quote():
    from trace.collectors.market_data.base import UnavailableMarketProvider
    from trace.collectors.market_data.confirmation import MarketConfirmer
    confirmer = MarketConfirmer(UnavailableMarketProvider("US"),
                                UnavailableMarketProvider("CN"))
    c = confirmer.confirm("US", "MU", "bullish")
    assert c.score == 5.0 and c.quote is None
    assert c.mode == "unavailable"
    assert confirmer.data_mode("US") == "unavailable"
