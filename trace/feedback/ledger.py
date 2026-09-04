"""预测回测账本（Forecast Ledger）：让系统对自己的预测负责。

Stage B 对每个证券输出方向预测（bullish/bearish），本模块把闭环补上：

    事件发生 → 记录锚点价 → horizon 小时后取真实行情 → 区间收益核对 → 落账

**必须是事件锚定的区间收益**，不是核对时刻的当日涨跌：
    收益 =（核对时刻价格 - 事件时刻锚点价）/ 锚点价 × 100%

    早期实现用的是 quote.change_pct_from_prev()，也就是"今天的日内涨跌"。
    长驻循环停三天再拉起来，核对的就是第三天那天的日内涨跌，却被当作
    "24 小时方向预测命中率"落账 —— 数字看起来正常，统计意义为零。

核对是**确定性规则**，不走 LLM：
    actual_direction = bullish   若区间收益 >= +move_threshold_pct
                     = bearish   若 <= -move_threshold_pct
                     = neutral   否则
    outcome = hit（同向）/ miss（反向）/ neutral（实际无方向）
            / unmeasurable（测不了，见下）

设计约束：
    - 锚点价来自 market_snapshot（MarketConfirmer 每次取报价都会落快照）。
      拿不到事件时刻附近的锚点价就是拿不到，不得用别的时刻的价格顶替。
    - 到期行情缺失时不落行、不消耗 impact，下轮重试（保持 pending）。
    - 每个 impact 只核对一次（forecast_check.impact_id UNIQUE）。
    - 只核对 bullish/bearish（neutral/uncertain 预测无从核对）。
    - unmeasurable 必须落账而不是留在 pending：否则事件早于快照采集的旧
      impact 会永远待核对，把 pending 变成只增不减的垃圾桶。它不计入命中率。
    - 生产门禁对齐：production 无行情接入时账本保持 pending，不产生假结果。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from trace.common.ids import forecast_check_id
from trace.db.connection import Database
from trace.db.repositories import ForecastCheckRepo, MarketSnapshotRepo, SecurityRepo
from trace.domain.models import ForecastCheck

logger = logging.getLogger(__name__)

OUTCOME_UNMEASURABLE = "unmeasurable"


class ForecastLedger:
    def __init__(self, db: Database, config, confirmer,
                 security_repo: SecurityRepo | None = None,
                 snapshot_repo: MarketSnapshotRepo | None = None):
        self.db = db
        self.confirmer = confirmer
        self.security_repo = security_repo or SecurityRepo(db)
        self.snapshots = snapshot_repo or MarketSnapshotRepo(db)
        self.repo = ForecastCheckRepo(db)
        self.horizon_hours = float(config.get("forecast.horizon_hours", 24))
        self.move_threshold_pct = float(
            config.get("forecast.move_threshold_pct", 1.0))
        # 锚点快照与事件时间的最大允许偏差
        self.anchor_tolerance_hours = float(
            config.get("forecast.anchor_tolerance_hours", 6))
        # 超过 horizon 这么多小时仍未核对 → 测到的已不是 horizon 收益
        self.max_delay_hours = float(config.get("forecast.max_delay_hours", 24))

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
            check = self._check_one(row, now, cutoff)
            if check is not None:
                self.repo.record(check)
                recorded.append(check)
        if recorded:
            logger.info("forecast ledger: %d checks recorded this round",
                        len(recorded))
        return recorded

    # ------------------------------------------------------------------
    def _check_one(self, row, now: datetime,
                   cutoff: datetime) -> ForecastCheck | None:
        """核对单条影响预测。返回 None 表示保持 pending（下轮重试）。"""
        ev_dt = _parse_dt(row["event_time"] or row["first_seen_at"])
        if ev_dt is None or ev_dt > cutoff:
            return None                     # 无事件时间 / 未到核对时点

        security = self.security_repo.get(row["security_id"])
        if security is None:
            return None
        elapsed_hours = (now - ev_dt).total_seconds() / 3600.0

        def unmeasurable(note: str, **fields) -> ForecastCheck:
            logger.info("forecast %s unmeasurable: %s", row["impact_id"], note)
            return self._build(row, ev_dt, now, elapsed_hours,
                               outcome=OUTCOME_UNMEASURABLE, note=note, **fields)

        # 1) 事件锚点价：快照只向前生成，此刻没有就永远不会有了
        anchor = self.snapshots.nearest(
            row["security_id"], ev_dt, self.anchor_tolerance_hours)
        if anchor is None or not anchor.last_price:
            return unmeasurable("no_anchor_snapshot")

        # 2) 到期价：真实行情缺失时保持 pending（不伪造核对结果）
        quote = self.confirmer.quote(security.market, security.ticker,
                                     security_id=security.security_id)
        exit_price = quote.last_price if quote else None
        if not exit_price:
            if elapsed_hours > self.horizon_hours + self.max_delay_hours:
                return unmeasurable("no_exit_quote",
                                    anchor_price=anchor.last_price,
                                    anchor_ts=anchor.ts)
            return None                     # 行情暂时不可用：下轮重试

        # 3) 核对严重超时：测到的已不是 horizon 收益，落账但不计入命中率
        if elapsed_hours > self.horizon_hours + self.max_delay_hours:
            return unmeasurable("check_delayed",
                                anchor_price=anchor.last_price,
                                anchor_ts=anchor.ts, exit_price=exit_price)

        # 4) 事件锚定区间收益
        change = (exit_price - anchor.last_price) / anchor.last_price * 100.0
        actual_direction = self._direction_of(change)
        predicted = row["direction"]
        if actual_direction == "neutral":
            outcome = "neutral"
        elif actual_direction == predicted:
            outcome = "hit"
        else:
            outcome = "miss"

        logger.info("forecast check %s: predicted=%s actual=%s(%.2f%% over %.1fh) → %s",
                    row["impact_id"], predicted, actual_direction, change,
                    elapsed_hours, outcome)
        return self._build(row, ev_dt, now, elapsed_hours, outcome=outcome,
                           actual_change_pct=round(change, 4),
                           actual_direction=actual_direction,
                           anchor_price=anchor.last_price, anchor_ts=anchor.ts,
                           exit_price=exit_price)

    # ------------------------------------------------------------------
    def _build(self, row, ev_dt: datetime, now: datetime, elapsed_hours: float,
               *, outcome: str, actual_change_pct: float | None = None,
               actual_direction: str = "", anchor_price: float | None = None,
               anchor_ts: datetime | None = None,
               exit_price: float | None = None, note: str = "") -> ForecastCheck:
        return ForecastCheck(
            check_id=forecast_check_id(),
            impact_id=row["impact_id"],
            event_id=row["event_id"],
            security_id=row["security_id"],
            predicted_direction=row["direction"],
            predicted_score=float(row["final_score"] or 0.0),
            confidence=float(row["confidence"] or 0.0),
            actual_change_pct=actual_change_pct,
            actual_direction=actual_direction,
            outcome=outcome,
            horizon_hours=self.horizon_hours,
            evaluated_at=now,
            anchor_price=anchor_price,
            anchor_ts=anchor_ts,
            exit_price=exit_price,
            elapsed_hours=round(elapsed_hours, 2),
            note=note,
        )

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
        if s["total"] == 0 and s["pending"] == 0 and not s.get("unmeasurable"):
            lines.append("还没有可核对的影响预测。")
            lines.append("系统会在事件发生 " + str(int(self.horizon_hours))
                         + " 小时后，用事件时刻锚点价与到期价的区间收益核对方向预测。")
            return "\n".join(lines)
        lines.append(f"已核对: {s['total']} ｜ ✅ 命中 {s['hits']} ｜ "
                     f"❌ 未命中 {s['misses']} ｜ ⚪ 实际中性 {s['neutrals']}")
        lines.append(f"待核对: {s['pending']}（等待行情数据/未到期）")
        if s.get("unmeasurable"):
            lines.append(f"无法核对: {s['unmeasurable']}（缺事件锚点价或核对超时，"
                         f"不计入命中率）")
        if s["hit_rate"] is None:
            lines.append("命中率: 暂无（还没有方向可判定的样本）")
        else:
            lines.append(f"命中率: {s['hit_rate'] * 100:.0f}%"
                         "（方向正确 / 有方向判定的核对）")
        lines.append("")
        lines.append(f"口径：事件时刻价 → {int(self.horizon_hours)} 小时后价格的区间收益，"
                     f"涨跌超过 ±{self.move_threshold_pct:g}% 才算有方向。")
        lines.append("说明：方向预测 ≠ 涨跌建议；样本量小时命中率参考意义有限。")
        return "\n".join(lines)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
