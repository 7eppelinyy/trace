"""Telegram 即时预警模板。

要求：短、快、可解释。
合规：事实重述 + AI生成中文摘要 + 来源名称 + 原文链接；
不重新发布付费媒体完整正文。

Confidence 与 Importance 分离展示：
重要度高 + 置信度低 → "⚠️ 高影响潜在事件，尚未获得官方确认"。
"""

from __future__ import annotations

from datetime import datetime

import pytz

from trace.db.connection import Database
from trace.db.repositories import EventSourceRepo, RawItemRepo, SecurityRepo, SourceRepo
from trace.domain.models import Event, EventImpact

_DIRECTION_EMOJI = {
    "bullish": "🟢 偏利多",
    "bearish": "🔴 偏利空",
    "neutral": "⚪ 中性",
    "mixed": "🟡 混合",
    "uncertain": "❓ 不确定",
}

# market_data_mode → 给用户看的说明（任务书 §7：降级必须显式标记）。
# "real" 不在表内（无需说明）。缺了任何一个取值，用户看到的就是一个
# 没有任何限定条件的重要度分数 —— 分数里的市场确认项其实是中性占位。
_MARKET_MODE_NOTES = {
    "mock": "行情为模拟数据（mock），市场确认仅供参考",
    "none": "无行情数据，未进行市场确认",
    # 生产模式未真实接入行情：禁止 Mock，只显示未接入
    "unavailable": "行情确认：暂未接入",
    "no_quote": "暂时取不到行情，本次未计入市场确认",
    # 交易时段门禁：事件之后市场还没开过盘 / 已过反应窗口，
    # 当前涨跌与本事件无关，市场确认取中性（见 market_data/confirmation.py）
    "market_not_opened_since_event": "市场尚未开盘，本次未计入市场确认（开盘后会重算）",
    "reaction_window_expired": "已超过事件反应窗口，本次未计入市场确认",
}


class AlertRenderer:
    def __init__(self, db: Database):
        self.db = db
        self.security_repo = SecurityRepo(db)
        self.source_repo = SourceRepo(db)
        self.raw_repo = RawItemRepo(db)
        self.event_source_repo = EventSourceRepo(db)

    # ------------------------------------------------------------------
    def render(self, event: Event, impact: EventImpact, user_timezone: str,
               is_update: bool = False) -> tuple[str, str | None]:
        """返回 (消息文本, 原文链接)。

        任务书 §12 必须显示：市场 / 证券代码 / 公司名称 / 方向 / 重要度 /
        置信度 / 事件状态 / 为什么重要 / 来源 / 原文 / 用户本地时间。
        任务书 §7：降级结果不得包装成完整真实分析，必须显式标注。
        """
        security = self.security_repo.get(impact.security_id)
        ticker = security.ticker if security else impact.security_id
        market_label = {"US": "美股", "CN": "A股"}.get(
            security.market if security else "", "未知市场")
        company = (security.company_name_zh or security.company_name_en
                   if security else "")

        direction = _DIRECTION_EMOJI.get(impact.direction, impact.direction)
        confidence_pct = int(round(impact.confidence * 100))

        # 状态行：重要度/置信度分离
        if event.status == "official_confirmed":
            status_line = "✅ 官方已确认"
        elif impact.final_score >= 8 and impact.confidence < 0.6:
            status_line = "⚠️ 高影响潜在事件，尚未获得官方确认"
        elif event.status == "rumor":
            status_line = "⚠️ 媒体报道/传闻，尚未获得官方确认"
        else:
            status_line = "⚠️ 媒体报道，尚未获得官方确认"

        header = "🔄 事件更新" if is_update else "🚨 重大事件"
        user_time = self._local_time(event.last_updated_at or event.first_seen_at,
                                     user_timezone)

        title_line = f"{ticker} 重大事件" if not company else f"{ticker} {company} 重大事件"
        lines = [
            f"{header}",
            title_line,
            "",
            f"市场：{market_label}｜代码：{ticker}",
            f"影响：{direction}",
            f"重要度：{impact.final_score:.1f} / 10",
            f"置信度：{confidence_pct}%",
            "",
            "事件",
            event.summary or event.title,
            "",
            "涉及证券",
            f"{ticker} {impact.final_score:.1f}（{impact.directness}）",
        ]

        if impact.reason:
            lines += ["", "为什么重要", impact.reason]
        if impact.industry_path:
            lines += ["", "产业传导", impact.industry_path]

        lines += ["", "状态", status_line]

        # 降级显式标记（§7）：不得把降级包装成完整真实分析
        degraded: list[str] = []
        if impact.analysis_mode == "rule_based_degraded":
            degraded.append("本次为规则降级分析（无 LLM），置信度受限")
        market_note = _MARKET_MODE_NOTES.get(impact.market_data_mode)
        if market_note:
            degraded.append(market_note)
        if degraded:
            lines += ["", "⚠️ " + "；".join(degraded)]

        # 来源：名称 + 时间 + 链接（只给链接，不重发正文）
        source_line, primary_url = self._source_line(event, user_timezone)
        if source_line:
            lines += ["", "来源", source_line]
        lines += ["", f"🕒 {user_time} 用户本地时间", "", f"Event ID: {event.event_id}"]

        return "\n".join(lines), primary_url

    # ------------------------------------------------------------------
    def _source_line(self, event: Event,
                     user_timezone: str) -> tuple[str, str | None]:
        primary = self.source_repo.get(event.primary_source_id or "")
        first = self.source_repo.get(event.first_source_id or "")
        names = []
        if first:
            names.append(first.source_name)
        if primary and (not first or primary.source_id != first.source_id):
            names.append(primary.source_name)
        url = None
        for es in self.event_source_repo.list_by_event(event.event_id):
            item = self.raw_repo.get(es.raw_item_id)
            if item and item.url:
                url = item.url
                if es.role in ("primary", "first"):
                    break
        # 来源时间也按用户时区显示：此前直接 strftime 出的是 UTC，
        # 却紧挨着下面那行"用户本地时间"，同一条消息里两个时区口径
        time_part = ""
        if event.first_seen_at:
            local = self._local_time(event.first_seen_at, user_timezone)
            if local:
                time_part = " · " + local[-5:]        # HH:MM
        return (" · ".join(names) + time_part if names else "", url)

    def _local_time(self, dt_utc: datetime | None, user_timezone: str) -> str:
        if dt_utc is None:
            return ""
        try:
            tz = pytz.timezone(user_timezone)
            if dt_utc.tzinfo is None:
                dt_utc = pytz.utc.localize(dt_utc)
            return dt_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return dt_utc.strftime("%Y-%m-%d %H:%M UTC")
