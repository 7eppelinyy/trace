"""跨市场交易时间测试：不能简单写 event + 30 minutes。"""

from datetime import datetime

import pytz

from trace.market_time.calendar import MarketCalendar


def test_next_open_crosses_timezone(db, config):
    cal = MarketCalendar(config)
    # 美国东部夜间（UTC 03:00 = NY 23:00 前一天，或当天盘后）发生事件
    dt = pytz.utc.localize(datetime(2026, 3, 10, 3, 0))   # 周二
    cn_open = cal.next_open("CN", dt)
    us_open = cal.next_open("US", dt)
    # A股开盘（北京 09:30 = UTC 01:30）应晚于事件时间
    assert cn_open > dt
    assert us_open > dt
    # A股开盘应在美股开盘之前（跨市场传播场景）
    assert cn_open < us_open


def test_weekend_skips_to_monday(db, config):
    cal = MarketCalendar(config)
    # 2026-03-14 是周六
    dt = pytz.utc.localize(datetime(2026, 3, 14, 12, 0))
    nxt = cal.next_open("US", dt)
    local = nxt.astimezone(pytz.timezone("America/New_York"))
    assert local.weekday() == 0   # 周一


def test_cn_holiday_skipped(db, config):
    cal = MarketCalendar(config)
    # 2026 国庆 10-01：事件在 9-30 晚上，下一个 A股开盘应跳过假期
    dt = pytz.utc.localize(datetime(2026, 9, 30, 16, 0))
    nxt = cal.next_open("CN", dt)
    assert nxt.date() > datetime(2026, 10, 1).date()


def test_reaction_window(db, config):
    cal = MarketCalendar(config)
    dt = pytz.utc.localize(datetime(2026, 3, 10, 3, 0))
    end = cal.reaction_window_end("CN", dt)
    open_at = cal.next_open("CN", dt)
    assert (end - open_at).total_seconds() == config.get("markets.reaction_window_minutes", 30) * 60
