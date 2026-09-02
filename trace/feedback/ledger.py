"""预测回测账本（Forecast Ledger）：让系统对自己的预测负责。

Stage B 对每个证券输出方向预测（bullish/bearish），但 Phase 1 此前
没有任何事后校验——预测对了错了无人知晓。本模块把闭环补上：

    事件发生 → 预测方向 → horizon 小时后取真实行情 → 方向核对 → 落账

核对是**确定性规则**，不走 LLM：
    actual_direction = bullish   若实际涨跌 >= +move_threshold_pct
                     = bearish   若 <= -move_threshold_pct
                     = neutral   否则
    outcome = hit（同向）/ miss（反向）/ neutral（实际无方向）

设计约束：
    - 行情缺失（quote 为 None 或无 prev_close）时不落行、不消耗 impact，
      下轮重试；行情长期不可用只体现为 pending 数不降。
    - 每个 impact 只核对一次（forecast_check.impact_id UNIQUE）。
    - 只核对 bullish/bearish（neutral/uncertain 预测无从核对）。
    - 生产门禁对齐：production 无行情接入时账本保持 pending，不产生假结果。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from trace.common.ids import forecast_check_id
from trace.db.connection import Database
from trace.db.repositories import ForecastCheckRepo, SecurityRepo
from trace.domain.models import ForecastCheck

logger = logging.getLogger(__name__)


class ForecastLedger:
    def __init__(self, db: Database, config, confirmer,
                 security_repo: SecurityRepo | None = None):
        self.db = db
        self.confirmer = confirmer
        self.security_repo = security_repo or SecurityRepo(db)
        self.repo = ForecastCheckRepo(db)
        self.horizon_hours = float(config.get("forecast.horizon_hours", 24))
        self.move_threshold_pct = float(
            config.get("forecast.move_threshold_pct", 1.0))

    # ------------------------------------------------------------------
    def run_due_checks(self, now: datetime | None = None) -> list[ForecastCheck]:
        """核对所有已到期且未落账的影响预测。返回本次新落账记录。"""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.horizon_hours)
        rows = self.db.query(
            """SELECT i.impact_id, i.event_id, i.security_id, i.direction,
                      i.final_score, i.confidence, e.event_time, e.first_seen_at
               FROM event_impact i
               JOIN event e ON e.event_id = i.event_id
               LEFT JOIN forecast_check f ON f.impact_id = i.impact_id
               WHERE f.impact_id IS NULL
                 AND i.direction IN ('bullish','bearish')""")

        recorded: list[ForecastCheck] = []
        for row in rows:
            event_time = row["event_time"] or row["first_seen_at"]
            if not event_time:
                continue
            try:
                ev_dt = datetime.fromisoformat(str(event_time))
            except ValueError:
                continue
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            if ev_dt > cutoff:
                continue    # 未到核对时点

            security = self.security_repo.get(row["security_id"])
            if security is None:
                continue
            quote = self.confirmer.quote(security.market, security.ticker)
            change = quote.change_pct_from_prev() if quote else None
            if change is None:
                continue    # 无真实行情：保持 pending，不伪造核对结果

            actual_direction = self._direction_of(change)
            predicted = row["direction"]
            if actual_direction == "neutral":
                outcome = "neutral"
            elif actual_direction == predicted:
                outcome = "hit"
            else:
                outcome = "miss"

            check = ForecastCheck(
                check_id=forecast_check_id(),
                impact_id=row["impact_id"],
                event_id=row["event_id"],
                security_id=row["security_id"],
                predicted_direction=predicted,
                predicted_score=float(row["final_score"] or 0.0),
                confidence=float(row["confidence"] or 0.0),
                actual_change_pct=round(change, 4),
                actual_direction=actual_direction,
                outcome=outcome,
                horizon_hours=self.horizon_hours,
                evaluated_at=now,
            )
            self.repo.record(check)
            recorded.append(check)
            logger.info("forecast check %s: predicted=%s actual=%s(%.2f%%) → %s",
                        row["impact_id"], predicted, actual_direction,
                        change, outcome)
        if recorded:
            logger.info("forecast ledger: %d checks recorded this round",
                        len(recorded))
        return recorded

    # ------------------------------------------------------------------
    def _direction_of(self, change_pct: float) -> str:
        if change_pct >= self.move_threshold_pct:
            return "bullish"
        if change_pct <= -self.move_threshold_pct:
            return "bearish"
        return "neutral"

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        """聚合统计（含待核对数），供 /accuracy 与 /status 使用。"""
        data = self.repo.summary()
        data["pending"] = self.repo.pending_count()
        return data

    def render_summary(self) -> str:
        """/accuracy 输出：诚实展示命中率，包括样本不足的情况。"""
        s = self.summary()
        lines = ["📈 预测回测账本", ""]
        if s["total"] == 0 and s["pending"] == 0:
            lines.append("还没有可核对的影响预测。")
            lines.append("系统会在事件发生 " + str(int(self.horizon_hours))
                         + " 小时后用真实行情核对方向预测。")
            return "\n".join(lines)
        lines.append(f"已核对: {s['total']} ｜ ✅ 命中 {s['hits']} ｜ "
                     f"❌ 未命中 {s['misses']} ｜ ⚪ 实际中性 {s['neutrals']}")
        lines.append(f"待核对: {s['pending']}（等待行情数据/未到期）")
        if s["hit_rate"] is None:
            lines.append("命中率: 暂无（还没有方向可判定的样本）")
        else:
            lines.append(f"命中率: {s['hit_rate'] * 100:.0f}%"
                         "（方向正确 / 有方向判定的核对）")
        lines.append("")
        lines.append("说明：方向预测 ≠ 涨跌建议；样本量小时命中率参考意义有限。")
        return "\n".join(lines)
