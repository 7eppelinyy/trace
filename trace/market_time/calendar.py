"""跨市场交易时间与日历管理 (F18/T12)。

美股和 A 股交易时间不同，不能简单写 `event + 30 minutes`。
必须支持：
    event_time → next_market_open → reaction_window

特性：
1. 多年份节假日表与覆盖范围检查 (提前 60 天预警)；
2. 特殊时段与提前收盘 (Early Close) 建模；
3. 超出日历覆盖范围或异常时抛出 CalendarUnavailableError，避免静默放行产生伪确认。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytz
import yaml

logger = logging.getLogger(__name__)

_HOLIDAYS_PATH = Path(__file__).parent / "holidays.yaml"


class CalendarUnavailableError(RuntimeError):
    """交易日历不可用或超出已知覆盖范围。"""
    pass


def _load_calendar_data() -> tuple[dict[str, set[date]], dict[str, dict[date, time]], dict[str, tuple[date, date]]]:
    if not _HOLIDAYS_PATH.exists():
        return {}, {}, {}
    with open(_HOLIDAYS_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    holidays: dict[str, set[date]] = {}
    early_closes: dict[str, dict[date, time]] = {}
    covered_range: dict[str, tuple[date, date]] = {}

    # 1. 覆盖范围
    for market, r in (raw.get("covered_range") or {}).items():
        try:
            start_d = date.fromisoformat(str(r["start"]))
            end_d = date.fromisoformat(str(r["end"]))
            covered_range[market] = (start_d, end_d)
        except Exception:
            pass

    # 2. 提前收盘
    for market, ec in (raw.get("early_closes") or {}).items():
        early_closes[market] = {}
        for d_str, t_str in (ec or {}).items():
            try:
                d = date.fromisoformat(str(d_str))
                t = time.fromisoformat(str(t_str))
                early_closes[market][d] = t
            except Exception:
                pass

    # 3. 休市日
    for market in ("US", "CN"):
        days = raw.get(market) or []
        holidays[market] = {date.fromisoformat(str(d)) for d in days if d}

    return holidays, early_closes, covered_range


class MarketCalendar:
    def __init__(self, config):
        self._markets: dict = config.get("markets", {}) or {}
        self._holidays, self._early_closes, self._covered_range = _load_calendar_data()
        self.reaction_window_minutes = int(config.get("markets.reaction_window_minutes", 30))

    # ------------------------------------------------------------------
    def knows_market(self, market: str) -> bool:
        """该市场是否有交易时段配置。"""
        return bool(self._markets.get(market, {}).get("sessions"))

    def is_covered(self, market: str, d: date) -> bool:
        """判断指定日期是否在已知日历覆盖范围内 (F18)。"""
        if market not in self._covered_range:
            # 如果没有明确写 covered_range，则检查节假日表中是否有当年数据
            known_years = {hd.year for hd in self._holidays.get(market, set())}
            return d.year in known_years
        start_d, end_d = self._covered_range[market]
        return start_d <= d <= end_d

    def check_coverage(self, market: str, days_ahead: int = 60) -> tuple[bool, str]:
        """提前至少 60 天监测未来日历覆盖 (T12 执行要求)。"""
        target_d = date.today() + timedelta(days=days_ahead)
        if self.is_covered(market, target_d):
            return True, f"Calendar for {market} is covered through {target_d.isoformat()}"
        msg = f"Trading calendar for {market} coverage missing at {target_d.isoformat()} (< {days_ahead} days ahead)"
        logger.warning(msg)
        return False, msg

    def market_tz(self, market: str) -> pytz.BaseTzInfo:
        tz_name = self._markets.get(market, {}).get("timezone", "UTC")
        return pytz.timezone(tz_name)

    def is_trading_day(self, market: str, d: date) -> bool:
        if d.weekday() >= 5:  # 周六、周日
            return False
        if not self.is_covered(market, d):
            raise CalendarUnavailableError(f"Date {d} is outside known calendar coverage for market {market}")
        return d not in self._holidays.get(market, set())

    def is_market_open(self, market: str, dt_utc: datetime) -> bool:
        """判断某 UTC 时刻该市场是否处于常规或特殊交易时段。"""
        cfg = self._markets.get(market)
        if not cfg:
            return False
        tz = self.market_tz(market)
        local = dt_utc.astimezone(tz)
        d = local.date()
        if not self.is_trading_day(market, d):
            return False

        t = local.time()
        early_close_t = self._early_closes.get(market, {}).get(d)

        for session in cfg.get("sessions", []):
            open_t = time.fromisoformat(session["open"])
            close_t = time.fromisoformat(session["close"])
            if early_close_t is not None and early_close_t < close_t:
                close_t = early_close_t
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
            try:
                is_trade = self.is_trading_day(market, day)
            except CalendarUnavailableError:
                raise
            if is_trade:
                for session in cfg.get("sessions", [{"open": "09:30", "close": "16:00"}]):
                    open_local = tz.localize(
                        datetime.combine(day, time.fromisoformat(session["open"])))
                    if open_local > local:
                        return open_local.astimezone(pytz.utc)
            day += timedelta(days=1)
        raise CalendarUnavailableError(f"Cannot find next open for market {market} within 14 days")

    def reaction_window_end(self, market: str, dt_utc: datetime) -> datetime:
        """反应窗口结束时刻：开盘后 reaction_window_minutes 分钟。"""
        open_utc = self.next_open(market, dt_utc)
        return open_utc + timedelta(minutes=self.reaction_window_minutes)


def next_market_open(calendar: MarketCalendar, market: str,
                     dt_utc: datetime) -> datetime:
    return calendar.next_open(market, dt_utc)
