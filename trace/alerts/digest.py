"""每日摘要（📊 美股 + A股每日事件摘要）。

至少包括：
- 今日最高风险事件
- Watchlist 每只股票涨跌
- 今日主要事件
- AI 综合事件判断
- 美股/A股跨市场关联
- 下一交易日重点事件
- 数据截止时间

日报不得预测"明天一定上涨/下跌"。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

import pytz

from trace.collectors.market_data.confirmation import MarketConfirmer
from trace.db.connection import Database
from trace.db.repositories import (
    DailyDigestRepo,
    EventImpactRepo,
    EventRepo,
    SecurityRepo,
)
from trace.common.ids import digest_id
from trace.domain.models import DailyDigest

logger = logging.getLogger(__name__)


class DigestBuilder:
    def __init__(self, db: Database, config, confirmer: MarketConfirmer):
        self.db = db
        self.config = config
        self.confirmer = confirmer
        self.digest_repo = DailyDigestRepo(db)
        self.event_repo = EventRepo(db)
        self.impact_repo = EventImpactRepo(db)
        self.security_repo = SecurityRepo(db)

    # ------------------------------------------------------------------
    def local_day_bounds(self, date_str: str,
                         timezone_name: str | None = None) -> tuple[datetime, datetime]:
        """把"用户时区的某一天"折算成 [start_utc, end_utc) 区间。

        日报标题、daily_digest 主键用的都是**用户本地日期**，而事件时间存的是
        UTC。不做这一步换算就会漏掉本地日凌晨的事件（Asia/Taipei 是当地
        00:00–08:00，恰好是美股收盘到盘后的窗口）。
        """
        tz = pytz.timezone(timezone_name
                           or self.config.telegram.default_user_timezone)
        local_start = tz.localize(
            datetime.combine(date.fromisoformat(date_str), time.min))
        return (local_start.astimezone(pytz.utc),
                tz.localize(datetime.combine(date.fromisoformat(date_str) + timedelta(days=1), time.min)).astimezone(pytz.utc))

    def build(self, date_str: str | None = None,
              timezone_name: str | None = None) -> DailyDigest:
        tz_name = timezone_name or self.config.telegram.default_user_timezone
        d = date_str or datetime.now(timezone.utc).astimezone(
            pytz.timezone(tz_name)).date().isoformat()
        start_utc, end_utc = self.local_day_bounds(d, tz_name)
        top_events = self.event_repo.top_between(start_utc, end_utc, limit=10)
        from trace.common.source_policy import event_permitted
        top_events = [ev for ev in top_events if event_permitted(self.db,ev.event_id,'display')
                      and event_permitted(self.db,ev.event_id,'forward')]

        lines = [f"📊 美股 + A股每日事件摘要（{d}）", ""]

        # 最高风险事件
        lines.append("【今日最高风险事件】")
        if top_events:
            for ev in top_events[:3]:
                impacts = self.impact_repo.list_by_event(ev.event_id)
                best = max(impacts, key=lambda i: i.final_score) if impacts else None
                score = f"{best.final_score:.1f}" if best else "-"
                lines.append(f"- [{score}] {ev.title}")
        else:
            lines.append("- 无重大事件")
        lines.append("")

        # Watchlist 涨跌（行情未接入时不得显示 Mock 数据）
        lines.append("【Watchlist 行情】")
        for sec in self.security_repo.list_watchlist_defaults():
            # 日报只展示当日涨跌，不做事件确认：不传 event_time（无需时段门禁）
            quote = self.confirmer.quote(sec.market, sec.ticker,
                                         security_id=sec.security_id)
            if quote is None:
                mode = self.confirmer.data_mode(sec.market)
                label = "行情确认：暂未接入" if mode == "unavailable" else "无行情数据"
                lines.append(f"- {sec.ticker}: {label}")
                continue
            pct = quote.change_pct_from_prev()
            pct_str = f"{pct:+.2f}%" if pct is not None else "N/A"
            lines.append(f"- {sec.ticker} ({sec.company_name_zh or sec.company_name_en}): {pct_str}")
        lines.append("")

        # 主要事件
        lines.append("【今日主要事件】")
        if top_events:
            for ev in top_events:
                lines.append(f"- [{ev.status}] {ev.title}")
        else:
            lines.append("- 无")
        lines.append("")

        # 跨市场关联
        lines.append("【跨市场关联观察】")
        lines.append("- 隔夜美股事件对今日 A股半导体/存储/服务器链的传导，请结合开盘表现判断。")
        lines.append("")

        # 明日观察
        lines.append("【下一交易日重点观察】")
        lines.append("- 事件后续官方确认情况、产业链价格与产能数据、相关公司公告。")
        lines.append("")

        # 合规声明：不预测涨跌
        lines.append("说明：本摘要只描述已发生事件与风险点，不构成涨跌预测。")
        # 标题用的是本地日期，截止时间也按同一时区显示（此前是 UTC，同一份
        # 摘要里两个时区口径，读者无从判断"今天"到底截到哪一刻）
        now_local = datetime.now(timezone.utc).astimezone(pytz.timezone(tz_name))
        lines.append(f"数据截止时间：{now_local.strftime('%Y-%m-%d %H:%M')} {tz_name}")
        lines.append(f"统计区间（UTC）：{start_utc.strftime('%m-%d %H:%M')} – "
                     f"{end_utc.strftime('%m-%d %H:%M')}")

        cache_key = f"{d}:{tz_name}:default:v1"
        digest = DailyDigest(
            digest_id=digest_id(),
            date_str=d,
            content_markdown="\n".join(lines),
            sent_at=datetime.now(timezone.utc),
            cache_key=cache_key,
        )
        self.digest_repo.upsert(digest)
        return digest
