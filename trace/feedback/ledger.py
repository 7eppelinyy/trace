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

from trace.common.ids import forecast_check_id, forecast_snapshot_id
from trace.db.connection import Database
from trace.db.repositories import (
    ForecastCheckRepo,
    ForecastSnapshotRepo,
    MarketSnapshotRepo,
    SecurityRepo,
)
from trace.domain.models import ForecastCheck, ForecastSnapshot

logger = logging.getLogger(__name__)

OUTCOME_UNMEASURABLE = "unmeasurable"


class ForecastLedger:
    def __init__(self, db: Database, config, confirmer,
                 security_repo: SecurityRepo | None = None,
                 snapshot_repo: MarketSnapshotRepo | None = None,
                 forecast_snapshot_repo: ForecastSnapshotRepo | None = None):
        self.db = db
        self.config = config
        self.confirmer = confirmer
        self.security_repo = security_repo or SecurityRepo(db)
        self.snapshots = snapshot_repo or MarketSnapshotRepo(db)
        self.snapshot_repo = forecast_snapshot_repo or ForecastSnapshotRepo(db)
        self.repo = ForecastCheckRepo(db)
        self.horizon_hours = float(config.get("forecast.horizon_hours", 24))
        self.move_threshold_pct = float(
            config.get("forecast.move_threshold_pct", 1.0))
        # 锚点快照与预测生成时间的最大允许偏差
        self.anchor_tolerance_hours = float(
            config.get("forecast.anchor_tolerance_hours", 6))
        # 超过 horizon 这么多小时仍未核对 → 测到的已不是 horizon 收益
        self.max_delay_hours = float(config.get("forecast.max_delay_hours", 24))
        self.model_version = str(config.get("forecast.model_version", "v1"))

    # ------------------------------------------------------------------
    def run_due_checks(self, now: datetime | None = None) -> list[ForecastCheck]:
        """核对所有已到期且未落账的影响预测快照。返回本次新落账记录。"""
        now = now or datetime.now(timezone.utc)
        # 1. 确保 legacy event_impact 同步创建不可变快照 (防遗留数据与测试直插 impact)
        self._sync_snapshots_from_impacts(now)

        # 2. 查询所有 status='pending' 且 due_at <= now 的预测快照
        due_snapshots = self.snapshot_repo.list_pending_due(now)

        recorded: list[ForecastCheck] = []
        for snap in due_snapshots:
            if snap.predicted_direction not in ('bullish','bearish'):
                self.snapshot_repo.update_status(snap.snapshot_id,'not_directional')
                continue
            check = self._check_snapshot(snap, now)
            if check is not None:
                self.repo.record(check)
                if check.outcome == OUTCOME_UNMEASURABLE:
                    self.snapshot_repo.update_status(snap.snapshot_id, "unmeasurable")
                else:
                    self.snapshot_repo.update_status(snap.snapshot_id, "evaluated")
                recorded.append(check)

        if recorded:
            logger.info("forecast ledger: %d checks recorded this round",
                        len(recorded))
        return recorded

    # ------------------------------------------------------------------
    def _sync_snapshots_from_impacts(self, now: datetime) -> None:
        """为尚未创建快照的有方向 event_impact 同步生成快照。"""
        rows = self.db.query(
            """SELECT i.impact_id, i.event_id, i.security_id, i.direction,
                      i.final_score, i.confidence, i.created_at AS impact_created_at,
                      e.event_time, e.first_seen_at, e.version AS event_version
               FROM event_impact i
               JOIN event e ON e.event_id = i.event_id
               LEFT JOIN forecast_snapshot s ON s.impact_id = i.impact_id
               WHERE s.snapshot_id IS NULL
                 AND i.direction IN ('bullish', 'bearish')"""
        )
        for r in rows:
            # A mutable impact is not evidence of a historical prediction. Record
            # discovery for audit, never backdate it to the underlying event.
            analysis_created = (
                _parse_dt(r["impact_created_at"])
                or now
            )
            sec = self.security_repo.get(r["security_id"])
            market = sec.market if sec else ("CN" if "CN" in r["security_id"] else "US")
            benchmark = "SPX" if market == "US" else "STAR50"

            anchor_snap = self.snapshots.nearest(
                r["security_id"], analysis_created, self.anchor_tolerance_hours)
            anchor_p = anchor_snap.last_price if anchor_snap else None
            anchor_t = anchor_snap.ts if anchor_snap else None

            bench_snap = self.snapshots.nearest(
                benchmark, analysis_created, self.anchor_tolerance_hours)
            bench_p = bench_snap.last_price if bench_snap else None

            due_at = analysis_created + timedelta(hours=self.horizon_hours)
            snap = ForecastSnapshot(
                snapshot_id=f"SNP-{r['impact_id']}",
                impact_id=r["impact_id"],
                event_id=r["event_id"],
                security_id=r["security_id"],
                event_version=r["event_version"] or 1,
                predicted_direction=r["direction"],
                predicted_score=float(r["final_score"] or 0.0),
                confidence=float(r["confidence"] or 0.0),
                model_version='legacy_unknown',
                market=market,
                analysis_created_at=analysis_created,
                published_at=None,
                anchor_price=None,
                anchor_ts=None,
                horizon_hours=self.horizon_hours,
                due_at=due_at,
                benchmark_code=benchmark,
                benchmark_anchor_price=bench_p,
                status="legacy_unverified",
                created_at=analysis_created,
            )
            self.snapshot_repo.insert(snap)
            self.db.execute("UPDATE forecast_snapshot SET time_basis='legacy_unverified' WHERE snapshot_id=?", (snap.snapshot_id,))

    # ------------------------------------------------------------------
    def _check_snapshot(self, snap: ForecastSnapshot, now: datetime) -> ForecastCheck | None:
        """核对单条不可变预测快照。返回 None 表示保持 pending（下轮重试）。"""
        provenance = self.db.query_one('SELECT time_basis FROM forecast_snapshot WHERE snapshot_id=?', (snap.snapshot_id,))
        if provenance and provenance['time_basis'] != 'analysis_recorded':
            self.snapshot_repo.update_status(snap.snapshot_id, 'legacy_unverified')
            return None
        if snap.analysis_created_at is None or now < snap.due_at:
            return None

        security = self.security_repo.get(snap.security_id)
        if security is None:
            return None

        # F19: 真实生命周期严格从 analysis_created_at (预测生成时点) 起算
        elapsed_hours = (now - snap.analysis_created_at).total_seconds() / 3600.0

        def unmeasurable(note: str, **fields) -> ForecastCheck:
            logger.info("forecast snapshot %s unmeasurable: %s", snap.snapshot_id, note)
            return self._build_check(snap, now, elapsed_hours,
                                     outcome=OUTCOME_UNMEASURABLE, note=note,
                                     excluded_reason=note, **fields)

        # 1) 预测生成时刻锚点价 (F19 防前视偏差核心)
        anchor_price = snap.anchor_price
        anchor_ts = snap.anchor_ts
        if anchor_price is None or not anchor_price:
            return unmeasurable("no_anchor_snapshot")
        if not anchor_ts or anchor_ts > snap.analysis_created_at:
            return unmeasurable('invalid_anchor_time')

        # 2) 到期价：真实行情缺失时保持 pending（不伪造核对结果）
        quote = self.confirmer.quote(security.market, security.ticker,
                                     security_id=security.security_id)
        from trace.collectors.market_data.time_quality import quote_quality
        if quote and quote_quality(quote, now) != 'real':
            if elapsed_hours > snap.horizon_hours + self.max_delay_hours:
                return unmeasurable('unreliable_exit_quote',anchor_price=anchor_price,anchor_ts=anchor_ts)
            return None
        exit_price = quote.last_price if quote else None
        if not exit_price:
            if elapsed_hours > snap.horizon_hours + self.max_delay_hours:
                return unmeasurable("no_exit_quote",
                                    anchor_price=anchor_price,
                                    anchor_ts=anchor_ts)
            return None                     # 行情暂时不可用：下轮重试

        # 3) 核对严重超时：测到的已不是 horizon 收益，落账但不计入命中率
        if elapsed_hours > snap.horizon_hours + self.max_delay_hours:
            return unmeasurable("check_delayed",
                                anchor_price=anchor_price,
                                anchor_ts=anchor_ts, exit_price=exit_price)

        # 4) 生成时点锚定区间收益 (杜绝前视偏差)
        change = (exit_price - anchor_price) / anchor_price * 100.0
        actual_direction = self._direction_of(change)
        predicted = snap.predicted_direction
        if actual_direction == "neutral":
            outcome = "neutral"
        elif actual_direction == predicted:
            outcome = "hit"
        else:
            outcome = "miss"

        # 5) 相对大盘基准超额收益 (F27)
        benchmark_code = snap.benchmark_code or ("SPX" if snap.market == "US" else "STAR50")
        bench_change = None
        excess_return = None

        bench_anchor = snap.benchmark_anchor_price
        if bench_anchor is None:
            b_snap = self.snapshots.nearest(
                benchmark_code, snap.analysis_created_at, self.anchor_tolerance_hours)
            if b_snap and b_snap.last_price and getattr(b_snap, 'source', '') != 'mock':
                ts = getattr(b_snap, 'market_ts', None) or getattr(b_snap, 'ts', None)
                if ts and ts <= snap.analysis_created_at and abs((snap.analysis_created_at - ts).total_seconds()) <= self.anchor_tolerance_hours * 3600:
                    bench_anchor = b_snap.last_price

        # 仅当存在基准锚点价且退出行情真实新鲜时才计算超额收益 (F27)
        if bench_anchor and bench_anchor > 0:
            b_quote = self.confirmer.quote(snap.market, benchmark_code,
                                           security_id=f"IDX-{benchmark_code}")
            from trace.collectors.market_data.time_quality import quote_quality
            if b_quote and quote_quality(b_quote, now) == 'real':
                bench_exit = b_quote.last_price
            else:
                bench_exit = None

            if bench_exit is None:
                b_snap_exit = self.snapshots.nearest(benchmark_code, now, self.anchor_tolerance_hours)
                if b_snap_exit and b_snap_exit.last_price and getattr(b_snap_exit, 'source', '') != 'mock':
                    ts_exit = getattr(b_snap_exit, 'market_ts', None) or getattr(b_snap_exit, 'ts', None)
                    if ts_exit and ts_exit <= now and abs((now - ts_exit).total_seconds()) <= self.anchor_tolerance_hours * 3600:
                        bench_exit = b_snap_exit.last_price

            if bench_exit and bench_exit > 0:
                bench_change = round((bench_exit - bench_anchor) / bench_anchor * 100.0, 4)
                excess_return = round(change - bench_change, 4)

        logger.info("forecast check %s (v%d): predicted=%s actual=%s(%.2f%% over %.1fh, excess=%s) → %s",
                    snap.snapshot_id, snap.event_version, predicted, actual_direction,
                    change, elapsed_hours, f"{excess_return}%" if excess_return is not None else "N/A",
                    outcome)

        return self._build_check(
            snap, now, elapsed_hours, outcome=outcome,
            actual_change_pct=round(change, 4),
            actual_direction=actual_direction,
            anchor_price=anchor_price, anchor_ts=anchor_ts,
            exit_price=exit_price,
            benchmark_code=benchmark_code,
            benchmark_change_pct=bench_change,
            excess_return_pct=excess_return,
        )

    # ------------------------------------------------------------------
    def _build_check(self, snap: ForecastSnapshot, now: datetime, elapsed_hours: float,
                    *, outcome: str, actual_change_pct: float | None = None,
                    actual_direction: str = "", anchor_price: float | None = None,
                    anchor_ts: datetime | None = None,
                    exit_price: float | None = None, note: str = "",
                    benchmark_code: str = "SPX",
                    benchmark_change_pct: float | None = None,
                    excess_return_pct: float | None = None,
                    excluded_reason: str = "") -> ForecastCheck:
        return ForecastCheck(
            check_id=forecast_check_id(),
            snapshot_id=snap.snapshot_id,
            impact_id=snap.impact_id,
            event_id=snap.event_id,
            security_id=snap.security_id,
            event_version=snap.event_version,
            predicted_direction=snap.predicted_direction,
            predicted_score=snap.predicted_score,
            confidence=snap.confidence,
            actual_change_pct=actual_change_pct,
            actual_direction=actual_direction,
            outcome=outcome,
            horizon_hours=snap.horizon_hours,
            evaluated_at=now,
            anchor_price=anchor_price,
            anchor_ts=anchor_ts,
            exit_price=exit_price,
            elapsed_hours=round(elapsed_hours, 2),
            note=note,
            model_version=snap.model_version,
            market=snap.market,
            benchmark_code=benchmark_code,
            benchmark_change_pct=benchmark_change_pct,
            excess_return_pct=excess_return_pct,
            excluded_reason=excluded_reason,
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
        """聚合统计（含待核对数与超额收益），供 /accuracy 与 /status 使用。"""
        data = self.repo.summary()
        data["pending"] = self.repo.pending_count()
        return data

    def render_summary(self) -> str:
        """/accuracy 输出：诚实、结构化分组展示命中率与超额收益。"""
        s = self.summary()
        lines = ["📈 预测回测账本 (防前视与基准超额评估)", ""]
        if s["total"] == 0 and s["pending"] == 0 and not s.get("unmeasurable"):
            lines.append("还没有可核对的影响预测。")
            lines.append("系统会在预测生成 " + str(int(self.horizon_hours))
                         + " 小时后，用生成时刻锚点价与到期价的区间收益核对方向预测。")
            return "\n".join(lines)

        lines.append(f"已核对: {s['total']} ｜ ✅ 命中 {s['hits']} ｜ "
                     f"❌ 未命中 {s['misses']} ｜ ⚪ 实际中性 {s['neutrals']}")
        lines.append(f"待核对: {s['pending']}（等待行情数据/未到期）")
        if s.get("unmeasurable"):
            lines.append(f"无法核对: {s['unmeasurable']}（缺生成锚点价或核对超时，"
                         f"不计入命中率）")

        m_rate = f"{s['measurement_rate'] * 100:.1f}%" if s.get("measurement_rate") is not None else "N/A"
        lines.append(f"实测率: {m_rate}（已实测 {s['total']} / 总预测快照 {s.get('total_snapshots', s['total'])}）")

        if s["hit_rate"] is None:
            lines.append("命中率: 暂无（还没有方向可判定的样本）")
        else:
            lines.append(f"命中率: {s['hit_rate'] * 100:.0f}%"
                         "（方向正确 / 有方向判定的核对）")

        if s.get("avg_excess_return") is not None:
            sign = "+" if s["avg_excess_return"] > 0 else ""
            lines.append(f"相对基准平均超额收益: {sign}{s['avg_excess_return']:.2f}% (US: SPX, CN: STAR50)")

        # 市场分组统计 (F27)
        by_mkt = s.get("by_market", {})
        if by_mkt:
            lines.append("")
            lines.append("【市场表现分组】")
            for mkt, data in by_mkt.items():
                mkt_label = "美股 (对标 SPX)" if mkt == "US" else "A股 (对标 STAR50)"
                hr = f"{data['hit_rate'] * 100:.1f}%" if data.get("hit_rate") is not None else "暂无"
                ex = f"{('+' if data['avg_excess_return'] > 0 else '')}{data['avg_excess_return']:.2f}%" if data.get("avg_excess_return") is not None else "无基准"
                lines.append(f"• {mkt_label}: 实测 {data['total']} ｜ 命中率 {hr} ｜ 超额 {ex}")

        # 方向分组统计 (F27)
        by_dir = s.get("by_direction", {})
        if by_dir:
            lines.append("")
            lines.append("【方向预测分组】")
            for d, data in by_dir.items():
                d_label = "看多 (Bullish)" if d == "bullish" else ("看空 (Bearish)" if d == "bearish" else d)
                hr = f"{data['hit_rate'] * 100:.1f}%" if data.get("hit_rate") is not None else "暂无"
                lines.append(f"• {d_label}: 实测 {data['total']} ｜ 命中率 {hr}")

        lines.append("")
        lines.append(f"口径：预测生成时刻价 → {int(self.horizon_hours)} 小时后价格的区间收益（杜绝前视偏差），"
                     f"涨跌超过 ±{self.move_threshold_pct:g}% 才算有方向。")
        lines.append("说明：方向预测 ≠ 涨跌建议；样本量小时命中率参考意义有限，禁止报喜不报忧。")
        return "\n".join(lines)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

