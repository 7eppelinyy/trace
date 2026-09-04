"""每日摘要的日期口径测试。

事件时间存的是 UTC，"今天"却是用户时区的概念。早期实现用
`date(first_seen_at) = <本地日期>` 直接比较两种口径，对 Asia/Taipei(+8)
会丢掉当地 00:00–08:00 的全部事件 —— 而那正好覆盖美股收盘到盘后，
是这套系统最不该漏的窗口（16:30 ET 的 8-K = 次日 04:30 台北）。

丢事件不会报错：摘要照常生成，只是少了几条。所以必须有测试盯着。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytz

from trace.db.repositories import EventImpactRepo, EventRepo
from trace.domain.models import Event, EventImpact

TAIPEI = pytz.timezone("Asia/Taipei")


def _seed(db, event_id: str, when_utc: datetime, *, score: float = 9.0,
          title: str = "SanDisk 盘后 8-K") -> None:
    EventRepo(db).insert(Event(
        event_id=event_id, title=title, summary=title, version=1,
        first_seen_at=when_utc, last_updated_at=when_utc, event_time=when_utc))
    EventImpactRepo(db).upsert(EventImpact(
        impact_id=f"IMP-{event_id}", event_id=event_id,
        security_id="SEC-US-SNDK", direction="bullish",
        base_score=score, final_score=score))


def test_after_hours_us_event_appears_in_local_digest(app):
    """16:30 ET 的盘后公告 = 次日 04:30 台北，必须出现在台北当天的摘要里。"""
    when = datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc)   # 16:30 ET
    local_date = when.astimezone(TAIPEI).date().isoformat()    # 2026-09-05
    assert local_date == "2026-09-05"

    _seed(app.db, "EV-AH", when)
    digest = app.digest_builder.build(local_date, "Asia/Taipei")

    assert "SanDisk 盘后 8-K" in digest.content_markdown
    assert digest.date_str == local_date


def test_events_outside_local_day_are_excluded(app):
    """区间是左闭右开的本地日：前一天 23:59 与次日 00:00 都不算今天。"""
    day_start = TAIPEI.localize(datetime(2026, 9, 5, 0, 0)).astimezone(pytz.utc)

    _seed(app.db, "EV-BEFORE", day_start - timedelta(minutes=1),
          title="前一天最后一分钟")
    _seed(app.db, "EV-FIRST", day_start, title="当天第一分钟")
    _seed(app.db, "EV-LAST", day_start + timedelta(hours=23, minutes=59),
          title="当天最后一分钟")
    _seed(app.db, "EV-AFTER", day_start + timedelta(days=1),
          title="次日第一分钟")

    content = app.digest_builder.build("2026-09-05", "Asia/Taipei").content_markdown
    assert "当天第一分钟" in content
    assert "当天最后一分钟" in content
    assert "前一天最后一分钟" not in content
    assert "次日第一分钟" not in content


def test_local_day_bounds_span_exactly_24h(app):
    start, end = app.digest_builder.local_day_bounds("2026-09-05", "Asia/Taipei")
    assert (end - start) == timedelta(days=1)
    # 台北 +8：本地 00:00 == 前一日 16:00 UTC
    assert start == datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc)


def test_bounds_follow_the_requested_timezone(app):
    """不同时区的"同一天"对应不同的 UTC 区间。"""
    tp_start, _ = app.digest_builder.local_day_bounds("2026-09-05", "Asia/Taipei")
    ny_start, _ = app.digest_builder.local_day_bounds("2026-09-05", "America/New_York")
    assert ny_start > tp_start
    assert (ny_start - tp_start) == timedelta(hours=12)     # +8 vs -4(EDT)


def test_digest_footer_states_the_window(app):
    """摘要里必须写清统计区间：否则读者无从判断"今天"截到哪一刻。"""
    _seed(app.db, "EV-1", datetime(2026, 9, 4, 20, 30, tzinfo=timezone.utc))
    content = app.digest_builder.build("2026-09-05", "Asia/Taipei").content_markdown
    assert "统计区间（UTC）" in content
    assert "Asia/Taipei" in content
