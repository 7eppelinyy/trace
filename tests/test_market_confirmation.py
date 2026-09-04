"""市场确认门禁 / 行情缓存 / 快照落库测试。

核心命题：`change_pct_from_prev()` 只是"当前价 vs 上一收盘"。它是否算作
对某个事件的市场反应，取决于事件之后市场有没有开过盘、以及过去多久了。
判断错了不会有任何报错——只会有一个被无关涨跌污染的 final_score
（市场确认权重默认 0.15，足以让事件跨过提醒阈值）。

用例特意选在 2026-09-04（周五）盘后：下一个美股交易日不是周一
（2026-09-07 是劳动节休市），而是周二 09-08。跨长周末不得被误判为"已过期"。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trace.collectors.market_data.base import MarketDataProvider, Quote
from trace.collectors.market_data.confirmation import (
    MODE_MARKET_NOT_OPENED,
    MODE_WINDOW_EXPIRED,
    MarketConfirmer,
)
from trace.db.repositories import MarketSnapshotRepo
from trace.market_time.calendar import MarketCalendar


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# 事件：2026-09-04（周五）18:00 ET = 22:00 UTC，美股已收盘
EVENT_AFTER_CLOSE = _utc("2026-09-04T22:00:00")
# 事件：2026-09-08（周二）10:00 ET = 14:00 UTC，处于交易时段
EVENT_IN_SESSION = _utc("2026-09-08T14:00:00")


class _CountingProvider(MarketDataProvider):
    """记录被调用次数的假行情源（涨 2%）。"""

    market = "US"
    data_mode = "real"

    def __init__(self, change_pct: float = 2.0):
        self.calls = 0
        self.change_pct = change_pct

    def get_quote(self, ticker: str) -> Quote | None:
        self.calls += 1
        prev = 100.0
        return Quote(ticker=ticker, ts=datetime.now(timezone.utc),
                     last_price=prev * (1 + self.change_pct / 100),
                     prev_close=prev)


class _NullCN(MarketDataProvider):
    market = "CN"
    data_mode = "unavailable"

    def get_quote(self, ticker: str) -> Quote | None:
        return None


def _confirmer(config, provider=None, **kwargs) -> MarketConfirmer:
    return MarketConfirmer(provider or _CountingProvider(), _NullCN(),
                           calendar=MarketCalendar(config), **kwargs)


# ---------------------------------------------------------------------------
# 交易时段门禁
# ---------------------------------------------------------------------------

def test_after_hours_event_before_next_open_is_neutral(config):
    """盘后事件、市场还没开过盘：当前报价反映的是事件之前的交易日。"""
    provider = _CountingProvider()
    c = _confirmer(config, provider).confirm(
        "US", "MU", "bullish",
        event_time=EVENT_AFTER_CLOSE,
        now=_utc("2026-09-05T12:00:00"))       # 周六

    assert c.score == 5.0, "无关涨跌不得进入 final_score"
    assert c.mode == MODE_MARKET_NOT_OPENED
    # 分数中性，但报价照样取回并落快照：盘后事件的回测锚点价正是这个收盘价
    assert c.quote is not None
    assert provider.calls == 1


def test_after_hours_event_confirmed_once_market_opens(config):
    """跨周末 + 劳动节休市后的首个交易日开盘，才谈得上市场确认。

    参考时刻是"事件后首次开盘"（2026-09-08 09:30 ET），不是事件时刻本身，
    否则长周末会让盘后事件直接过期。
    """
    c = _confirmer(config).confirm(
        "US", "MU", "bullish",
        event_time=EVENT_AFTER_CLOSE,
        now=_utc("2026-09-08T13:45:00"))       # 周二开盘后 15 分钟

    assert c.mode == "real"
    assert c.score > 5.0                        # 预测看涨 + 实际上涨 → 加分
    assert c.quote is not None


def test_stale_event_no_longer_confirmable(config):
    """开盘后超过反应窗口：今天的日内涨跌不再是对该事件的反应。"""
    c = _confirmer(config).confirm(
        "US", "MU", "bullish",
        event_time=EVENT_AFTER_CLOSE,
        now=_utc("2026-09-09T20:00:00"))       # 首次开盘后约 30.5h

    assert c.score == 5.0
    assert c.mode == MODE_WINDOW_EXPIRED


def test_intraday_event_is_confirmed(config):
    """事件发生在交易时段内：当日涨跌确实包含该事件，正常确认。"""
    c = _confirmer(config).confirm(
        "US", "MU", "bearish",
        event_time=EVENT_IN_SESSION,
        now=_utc("2026-09-08T17:00:00"))

    assert c.mode == "real"
    assert c.score < 5.0                        # 预测看跌 + 实际上涨 → 扣分


def test_no_event_time_keeps_legacy_behaviour(config):
    """不传 event_time（如日报当日涨跌）：不做时段门禁。"""
    c = _confirmer(config).confirm("US", "MU", "bullish")
    assert c.mode == "real"
    assert c.quote is not None


def test_gate_disabled_without_calendar(config):
    """没有日历（旧构造方式）时保持既有行为，不静默把一切变中性。"""
    c = MarketConfirmer(_CountingProvider(), _NullCN()).confirm(
        "US", "MU", "bullish",
        event_time=EVENT_AFTER_CLOSE, now=_utc("2026-09-05T12:00:00"))
    assert c.mode == "real"
    assert c.score > 5.0


def test_unavailable_provider_is_neutral_and_marked(config):
    """行情未接入：中性 5 分且显式标记，不得伪装成真实市场确认。"""
    c = _confirmer(config).confirm("CN", "000001", "bullish")
    assert c.score == 5.0
    assert c.mode == "unavailable"


# ---------------------------------------------------------------------------
# TTL 缓存
# ---------------------------------------------------------------------------

def test_quote_cache_avoids_repeated_provider_calls(config):
    """同一轮里同一 ticker 会被 Stage B / 日报 / 回测反复请求。"""
    provider = _CountingProvider()
    confirmer = _confirmer(config, provider, cache_ttl_seconds=300)

    for _ in range(5):
        confirmer.quote("US", "MU")
    assert provider.calls == 1

    confirmer.invalidate_cache()               # 下一轮强制重取
    confirmer.quote("US", "MU")
    assert provider.calls == 2


def test_quote_cache_is_per_ticker(config):
    provider = _CountingProvider()
    confirmer = _confirmer(config, provider, cache_ttl_seconds=300)
    confirmer.quote("US", "MU")
    confirmer.quote("US", "SNDK")
    assert provider.calls == 2


def test_zero_ttl_disables_cache(config):
    provider = _CountingProvider()
    confirmer = _confirmer(config, provider, cache_ttl_seconds=0)
    confirmer.quote("US", "MU")
    confirmer.quote("US", "MU")
    assert provider.calls == 2


# ---------------------------------------------------------------------------
# 快照落库
# ---------------------------------------------------------------------------

def test_quote_persists_market_snapshot(db, config):
    """取到的行情必须留下价格历史：事件锚定回测与事后审计都依赖它。"""
    repo = MarketSnapshotRepo(db)
    confirmer = _confirmer(config, snapshot_repo=repo)

    assert repo.latest("SEC-US-MU") is None
    quote = confirmer.quote("US", "MU", security_id="SEC-US-MU")

    snapshot = repo.latest("SEC-US-MU")
    assert snapshot is not None
    assert snapshot.last_price == pytest.approx(quote.last_price)
    assert snapshot.prev_close == pytest.approx(100.0)


def test_snapshot_failure_does_not_break_confirmation(db, config):
    """快照落库失败是旁路问题，不得让市场确认整体失败。"""
    class _BrokenRepo:
        def insert(self, snapshot):
            raise RuntimeError("disk full")

    confirmer = _confirmer(config, snapshot_repo=_BrokenRepo())
    assert confirmer.quote("US", "MU", security_id="SEC-US-MU") is not None
