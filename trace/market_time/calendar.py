"""跨市场交易时间管理。

美股和A股交易时间不同，不能简单写 `event + 30 minutes`。
必须支持：

    event_time → next_market_open → reaction_window

例如：美国夜间 NVDA 重大事件 → A股休市 → 次日上午 A股开盘 →
观察国产 GPU/服务器/光模块/半导体产业链反应。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml
import pytz

_HOLIDAYS_PATH = Path(__file__).parent / "holidays.yaml"


def _load_holidays() -> dict[str, set[date]]:
    if not _HOLIDAYS_PATH.exists():
        return {}
    with open(_HOLIDAYS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, set[date]] = {}
    for market, days in raw.items():
        out[market] = {date.fromisoformat(str(d)) for d in days or []}
    return out


class MarketCalendar:
    def __init__(self, config):
        self._markets: dict = config.get("markets", {}) or {}
        self._holidays = _load_holidays()
        self.reaction_window_minutes = int(config.get("markets.reaction_window_minutes", 30))

    # ------------------------------------------------------------------
    def knows_market(self, market: str) -> bool:
        """该市场是否有交易时段配置（无配置时调用方不应做时段门禁）。"""
        return bool(self._markets.get(market, {}).get("sessions"))

    def market_tz(self, market: str) -> pytz.BaseTzInfo:
        tz_name = self._markets.get(market, {}).get("timezone", "UTC")
        return pytz.timezone(tz_name)

    def is_trading_day(self, market: str, d: date) -> bool:
        if d.weekday() >= 5:  # 周末
            return False
        return d not in self._holidays.get(market, set())

    def is_market_open(self, market: str, dt_utc: datetime) -> bool:
        """判断某 UTC 时刻该市场是否处于常规交易时段。"""
        cfg = self._markets.get(market)
        if not cfg:
            return False
        tz = self.market_tz(market)
        local = dt_utc.astimezone(tz)
        if not self.is_trading_day(market, local.date()):
            return False
        t = local.time()
        for session in cfg.get("sessions", []):
            open_t = time.fromisoformat(session["open"])
            close_t = time.fromisoformat(session["close"])
            if open_t <= t < close_t:
                return True
        return False

    # ------------------------------------------------------------------
    def next_open(self, market: str, dt_utc: datetime) -> datetime:
        """返回事件时间之后该市场下一次开盘时刻（UTC）。"""
        cfg = self._markets.get(market, {})
        tz = self.market_tz(market)
        local = dt_utc.astimezone(tz)
        day = local.date()
        for _ in range(14):  # 最多向后找两周
            if self.is_trading_day(market, day):
                for session in cfg.get("sessions", [{"open": "09:30", "close": "16:00"}]):
                    open_local = tz.localize(
                        datetime.combine(day, time.fromisoformat(session["open"])))
                    if open_local > local:
                        return open_local.astimezone(pytz.utc)
            day += timedelta(days=1)
        raise RuntimeError(f"cannot find next open for {market}")

    def reaction_window_end(self, market: str, dt_utc: datetime) -> datetime:
        """反应窗口结束时刻：开盘后 reaction_window_minutes 分钟。"""
        open_utc = self.next_open(market, dt_utc)
        return open_utc + timedelta(minutes=self.reaction_window_minutes)


def next_market_open(calendar: MarketCalendar, market: str,
                     dt_utc: datetime) -> datetime:
    return calendar.next_open(market, dt_utc)
