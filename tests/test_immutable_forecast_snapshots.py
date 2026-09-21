"""不可变预测快照与防前视回测评估测试 (T13 / F19 / F27)。

验证核心：
1. 不可变快照与版本隔离：事件修订生成新快照，历史快照与评估结果完全保持不变；
2. 彻底杜绝前视偏差 (F19)：基准价格严格锚定预测生成时点 (analysis_created_at)，
   而非原始 event_time；预测产生前已发生的涨幅不计入收益；
3. 多市场基准超额收益 (F27)：US 对标 SPX，CN 对标 STAR50；
4. 结构化分组评估与透明样本率 (F27)：按模型、市场、方向多维统计，实测率/失效占比透明输出；
5. Telegram /accuracy 渲染输出对齐。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trace.collectors.market_data.base import Quote
from trace.domain.models import Event, EventImpact, ForecastSnapshot, MarketSnapshot
from trace.db.repositories import (
    EventImpactRepo,
    EventRepo,
    ForecastCheckRepo,
    ForecastSnapshotRepo,
    MarketSnapshotRepo,
)
from trace.feedback.ledger import ForecastLedger


class _MultiConfirmer:
    """支持个股与基准指数报价的假行情提供方。"""

    def __init__(self, quotes_map: dict[str, float], *, as_of: datetime | None = None,
                 market_ts: datetime | None = None, is_delayed: bool = False):
        self.quotes_map = quotes_map  # ticker -> last_price
        self.as_of = as_of
        self.market_ts = market_ts
        self.is_delayed = is_delayed
        self.calls: list[tuple[str, str]] = []

    def quote(self, market: str, ticker: str, *,
              security_id: str | None = None) -> Quote | None:
        self.calls.append((market, ticker))
        price = self.quotes_map.get(ticker) or self.quotes_map.get(ticker.split(".")[0])
        if price is None:
            return None
        t = self.as_of or datetime.now(timezone.utc)
        m_t = self.market_ts if self.market_ts is not None else t
        return Quote(
            ticker=ticker,
            ts=t,
            market_timestamp=m_t,
            last_price=price,
            prev_close=price,
            is_delayed=self.is_delayed,
        )


def _setup_event_and_snapshots(db, *, event_id="EVT-TEST", security_id="SEC-US-MU",
                                market="US", direction="bullish",
                                event_time: datetime, analysis_time: datetime,
                                anchor_price: float,
                                benchmark_code="SPX", benchmark_anchor_price: float | None = None,
                                event_version: int = 1, horizon_hours: float = 24.0) -> ForecastSnapshot:
    EventRepo(db).insert(Event(
        event_id=event_id,
        title="Test Memory Market Update",
        version=event_version,
        event_time=event_time,
        first_seen_at=event_time,
    ))
    impact_id = f"IMP-{event_id}-v{event_version}"
    EventImpactRepo(db).upsert(EventImpact(
        impact_id=impact_id,
        event_id=event_id,
        security_id=security_id,
        direction=direction,
        confidence=0.8,
        final_score=8.5,
    ))
    snap_repo = ForecastSnapshotRepo(db)
    snapshot = ForecastSnapshot(
        snapshot_id=f"SNAP-{event_id}-v{event_version}",
        impact_id=impact_id,
        event_id=event_id,
        security_id=security_id,
        event_version=event_version,
        predicted_direction=direction,
        predicted_score=8.5,
        confidence=0.8,
        model_version="v1",
        market=market,
        analysis_created_at=analysis_time,
        published_at=analysis_time,
        anchor_price=anchor_price,
        anchor_ts=analysis_time,
        horizon_hours=horizon_hours,
        due_at=analysis_time + timedelta(hours=horizon_hours),
        benchmark_code=benchmark_code,
        benchmark_anchor_price=benchmark_anchor_price,
        status="pending",
        created_at=analysis_time,
    )
    snap_repo.insert(snapshot)
    return snapshot


def test_immutable_snapshots_on_revision(db, config):
    """验收标准 1：存在新事件修订场景时，历史快照与新快照并存，且历史评估结果不发生变动。"""
    now = datetime.now(timezone.utc)
    t_v1 = now - timedelta(hours=30.0)

    # 1. 建立 v1 快照（30 小时前生成，已到期）
    _setup_event_and_snapshots(
        db,
        event_id="EVT-REV-1",
        security_id="SEC-US-MU",
        direction="bullish",
        event_time=t_v1,
        analysis_time=t_v1,
        anchor_price=100.0,
        event_version=1,
    )

    # 2. 执行第一次核对 (MU 从 100 涨到 105, 预测命中)
    confirmer_v1 = _MultiConfirmer({"MU": 105.0}, as_of=now)
    ledger = ForecastLedger(db, config, confirmer_v1)
    checks_v1 = ledger.run_due_checks(now=now)
    assert len(checks_v1) == 1
    assert checks_v1[0].event_version == 1
    assert checks_v1[0].outcome == "hit"
    assert checks_v1[0].actual_change_pct == pytest.approx(5.0)

    # 3. 发生事件修订 (版本升级为 2，产生新的不可变快照)
    t_v2 = now - timedelta(hours=2.0)
    EventRepo(db).update(Event(
        event_id="EVT-REV-1",
        title="Test Memory Market Update (Revised)",
        version=2,
        event_time=t_v1,
        first_seen_at=t_v1,
    ))
    snap_repo = ForecastSnapshotRepo(db)
    snap_v2 = ForecastSnapshot(
        snapshot_id="SNAP-EVT-REV-1-v2",
        impact_id="IMP-EVT-REV-1-v2",
        event_id="EVT-REV-1",
        security_id="SEC-US-MU",
        event_version=2,
        predicted_direction="bearish",  # 修订为反向
        predicted_score=4.0,
        confidence=0.6,
        model_version="v1",
        market="US",
        analysis_created_at=t_v2,
        published_at=t_v2,
        anchor_price=106.0,
        anchor_ts=t_v2,
        horizon_hours=24.0,
        due_at=t_v2 + timedelta(hours=24.0),
        benchmark_code="SPX",
        status="pending",
        created_at=t_v2,
    )
    snap_repo.insert(snap_v2)

    # 4. 验证：历史快照与新快照同时存在，且 v1 状态已为 evaluated，v2 状态为 pending
    all_snaps = snap_repo.list_by_event("EVT-REV-1")
    assert len(all_snaps) == 2
    assert all_snaps[0].event_version == 1
    assert all_snaps[0].status == "evaluated"
    assert all_snaps[1].event_version == 2
    assert all_snaps[1].status == "pending"

    # 5. 再次运行账本：v2 尚未到期 (due_at 在 22 小时后)，不会重复核对 v1，也不会提前核对 v2
    checks_v2 = ledger.run_due_checks(now=now)
    assert len(checks_v2) == 0

    # 6. 时间推进到 v2 到期时，核对 v2
    future_now = t_v2 + timedelta(hours=25.0)
    confirmer_v2 = _MultiConfirmer({"MU": 102.0}, as_of=future_now)  # 106 -> 102, 下跌 -3.77%, bearish 命中
    ledger_v2 = ForecastLedger(db, config, confirmer_v2)
    checks_final = ledger_v2.run_due_checks(now=future_now)
    assert len(checks_final) == 1
    assert checks_final[0].event_version == 2
    assert checks_final[0].outcome == "hit"
    assert checks_final[0].predicted_direction == "bearish"

    # 历史评估记录没有被覆盖
    history_checks = db.query(
        "SELECT * FROM forecast_check WHERE event_id='EVT-REV-1' ORDER BY event_version ASC"
    )
    assert len(history_checks) == 2
    assert history_checks[0]["event_version"] == 1
    assert history_checks[0]["predicted_direction"] == "bullish"
    assert history_checks[1]["event_version"] == 2
    assert history_checks[1]["predicted_direction"] == "bearish"


def test_no_lookahead_bias_when_analysis_delayed(db, config):
    """验收标准 2：彻底消灭 F19 前视偏差。
    
    场景：
    - 事件发生于 30 小时前 (当时行情 100.0)；
    - 在事件发生后 6 小时内，价格已经暴涨到 108.0 (+8.0%)；
    - Stage B 在 24 小时前运行分析 (analysis_created_at = 24h 前)，以 108.0 为基准预测 bullish；
    - 24 小时后核对 (当前时点)，价格微涨至 108.5 (+0.46%)；
    - 若按旧逻辑 (错误以事件发生时刻 100.0 锚定)：
        涨跌幅 = (108.5 - 100.0) / 100.0 = +8.5% -> 假命中 (将预测前已发生的暴涨揽为模型功劳，典型前视偏差)！
    - 按新逻辑 (以预测生成时刻 108.0 锚定)：
        涨跌幅 = (108.5 - 108.0) / 108.0 = +0.46% -> 低于 1.0% 阈值，判为 neutral (未达显著区间涨幅)！
    """
    now = datetime.now(timezone.utc)
    event_time = now - timedelta(hours=30.0)
    analysis_time = now - timedelta(hours=24.0)

    # 落事件时刻行情快照 (100.0)
    MarketSnapshotRepo(db).insert(MarketSnapshot(
        security_id="SEC-US-MU",
        ts=event_time,
        last_price=100.0,
        prev_close=100.0,
    ))
    # 落预测生成时刻行情快照 (108.0)
    MarketSnapshotRepo(db).insert(MarketSnapshot(
        security_id="SEC-US-MU",
        ts=analysis_time,
        last_price=108.0,
        prev_close=100.0,
    ))

    # 建立预测快照，严密锚定 analysis_created_at 与 anchor_price=108.0
    _setup_event_and_snapshots(
        db,
        event_id="EVT-F19-TEST",
        security_id="SEC-US-MU",
        direction="bullish",
        event_time=event_time,
        analysis_time=analysis_time,
        anchor_price=108.0,
        horizon_hours=24.0,
    )

    confirmer = _MultiConfirmer({"MU": 108.5})
    ledger = ForecastLedger(db, config, confirmer)
    checks = ledger.run_due_checks(now=now)

    assert len(checks) == 1
    c = checks[0]
    # 锚点价必须是预测时点的 108.0，绝不能是事件时点的 100.0
    assert c.anchor_price == pytest.approx(108.0)
    assert c.exit_price == pytest.approx(108.5)
    assert c.actual_change_pct == pytest.approx(0.463, abs=1e-3)
    # 判定为 neutral，杜绝前视伪命中
    assert c.actual_direction == "neutral"
    assert c.outcome == "neutral"


def test_benchmark_excess_return_us_and_cn(db, config):
    """验收标准 3：美股与 A 股不同基准下的超额收益计算 (F27)。"""
    now = datetime.now(timezone.utc)
    t_past = now - timedelta(hours=25.0)

    # 1. 美股样本 (MU vs SPX)
    # MU: 100 -> 105 (+5.0%)
    # SPX: 5000 -> 5100 (+2.0%)
    # 预期超额收益: +5.0% - +2.0% = +3.0%
    _setup_event_and_snapshots(
        db,
        event_id="EVT-BENCH-US",
        security_id="SEC-US-MU",
        market="US",
        direction="bullish",
        event_time=t_past,
        analysis_time=t_past,
        anchor_price=100.0,
        benchmark_code="SPX",
        benchmark_anchor_price=5000.0,
    )

    # 2. A股样本 (688981 中芯国际 vs STAR50)
    # 688981: 50.0 -> 47.5 (-5.0%, 看空命中)
    # STAR50: 1000.0 -> 990.0 (-1.0%)
    # 预期超额收益: -5.0% - (-1.0%) = -4.0%
    _setup_event_and_snapshots(
        db,
        event_id="EVT-BENCH-CN",
        security_id="SEC-CN-688981.SH",
        market="CN",
        direction="bearish",
        event_time=t_past,
        analysis_time=t_past,
        anchor_price=50.0,
        benchmark_code="STAR50",
        benchmark_anchor_price=1000.0,
    )

    confirmer = _MultiConfirmer({
        "MU": 105.0,
        "SPX": 5100.0,
        "688981": 47.5,
        "STAR50": 990.0,
    })

    ledger = ForecastLedger(db, config, confirmer)
    checks = ledger.run_due_checks(now=now)
    assert len(checks) == 2

    us_check = next(c for c in checks if c.market == "US")
    assert us_check.actual_change_pct == pytest.approx(5.0)
    assert us_check.benchmark_change_pct == pytest.approx(2.0)
    assert us_check.excess_return_pct == pytest.approx(3.0)
    assert us_check.outcome == "hit"

    cn_check = next(c for c in checks if c.market == "CN")
    assert cn_check.actual_change_pct == pytest.approx(-5.0)
    assert cn_check.benchmark_change_pct == pytest.approx(-1.0)
    assert cn_check.excess_return_pct == pytest.approx(-4.0)
    assert cn_check.outcome == "hit"


def test_grouped_summary_and_sample_transparency(db, config):
    """验收标准 4：结构化多维统计、样本总量、实测率、中性/失效样本占比 (F27)。"""
    now = datetime.now(timezone.utc)
    t_past = now - timedelta(hours=25.0)

    # 插入 4 个样本：
    # 1: US bullish 命中, 超额 +2.0%
    _setup_event_and_snapshots(
        db, event_id="E1", security_id="SEC-US-MU", market="US", direction="bullish",
        event_time=t_past, analysis_time=t_past, anchor_price=100.0,
        benchmark_code="SPX", benchmark_anchor_price=5000.0,
    )
    # 2: US bearish 反向未命中 (实际大涨), 超额 +4.0%
    _setup_event_and_snapshots(
        db, event_id="E2", security_id="SEC-US-NVDA", market="US", direction="bearish",
        event_time=t_past, analysis_time=t_past, anchor_price=100.0,
        benchmark_code="SPX", benchmark_anchor_price=5000.0,
    )
    # 3: CN bearish 命中, 超额 -3.0%
    _setup_event_and_snapshots(
        db, event_id="E3", security_id="SEC-CN-688981.SH", market="CN", direction="bearish",
        event_time=t_past, analysis_time=t_past, anchor_price=50.0,
        benchmark_code="STAR50", benchmark_anchor_price=1000.0,
    )
    # 4: CN bullish 实际无波动 (neutral)
    _setup_event_and_snapshots(
        db, event_id="E4", security_id="SEC-CN-688012.SH", market="CN", direction="bullish",
        event_time=t_past, analysis_time=t_past, anchor_price=100.0,
        benchmark_code="STAR50", benchmark_anchor_price=1000.0,
    )

    confirmer = _MultiConfirmer({
        "MU": 104.0,       # +4.0% (SPX +2.0% -> 超额 +2.0%)
        "NVDA": 106.0,     # +6.0% (SPX +2.0% -> 超额 +4.0%)
        "SPX": 5100.0,     # +2.0%
        "688981": 48.0,    # -4.0% (STAR50 -1.0% -> 超额 -3.0%)
        "688012": 100.2,   # +0.2% (neutral)
        "STAR50": 990.0,   # -1.0%
    })

    ledger = ForecastLedger(db, config, confirmer)
    ledger.run_due_checks(now=now)

    summary = ledger.summary()
    assert summary["total"] == 4
    assert summary["hits"] == 2      # E1 (US bull hit), E3 (CN bear hit)
    assert summary["misses"] == 1    # E2 (US bear miss)
    assert summary["neutrals"] == 1  # E4 (CN neutral)
    assert summary["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert summary["measurement_rate"] == 1.0
    assert summary["neutral_rate"] == 0.25

    # 平均超额收益 (命中+未命中 = E1(+2.0) + E2(+4.0) + E3(-3.0)) / 3 = +1.0%
    assert summary["avg_excess_return"] == pytest.approx(1.0, abs=1e-2)

    # 市场分组
    by_mkt = summary["by_market"]
    assert "US" in by_mkt and "CN" in by_mkt
    assert by_mkt["US"]["total"] == 2
    assert by_mkt["US"]["hits"] == 1
    assert by_mkt["US"]["misses"] == 1
    assert by_mkt["US"]["hit_rate"] == 0.5
    assert by_mkt["US"]["avg_excess_return"] == pytest.approx(3.0)

    assert by_mkt["CN"]["total"] == 2
    assert by_mkt["CN"]["hits"] == 1
    assert by_mkt["CN"]["neutrals"] == 1
    assert by_mkt["CN"]["hit_rate"] == 1.0
    assert by_mkt["CN"]["avg_excess_return"] == pytest.approx(-3.0)

    # 方向分组
    by_dir = summary["by_direction"]
    assert "bullish" in by_dir and "bearish" in by_dir
    assert by_dir["bullish"]["hits"] == 1
    assert by_dir["bullish"]["neutrals"] == 1
    assert by_dir["bearish"]["hits"] == 1
    assert by_dir["bearish"]["misses"] == 1


def test_telegram_accuracy_render_output(db, config):
    """验收标准 5：Telegram /accuracy 格式完整、包含分组、口径与诚实指标。"""
    now = datetime.now(timezone.utc)
    t_past = now - timedelta(hours=25.0)

    _setup_event_and_snapshots(
        db, event_id="E_TG_1", security_id="SEC-US-MU", market="US", direction="bullish",
        event_time=t_past, analysis_time=t_past, anchor_price=100.0,
        benchmark_code="SPX", benchmark_anchor_price=5000.0,
    )

    confirmer = _MultiConfirmer({"MU": 103.0, "SPX": 5050.0})
    ledger = ForecastLedger(db, config, confirmer)
    ledger.run_due_checks(now=now)

    text = ledger.render_summary()
    assert "📈 预测回测账本" in text
    assert "实测率" in text
    assert "相对基准平均超额收益" in text
    assert "美股 (对标 SPX)" in text
    assert "看多 (Bullish)" in text
    assert "杜绝前视偏差" in text
    assert "禁止报喜不报忧" in text


def test_stale_exit_quote_handling(db, config):
    """反向用例：刻意将 market_timestamp 留在旧时间，断言未超最大延迟时保持 pending；
    超过允许延迟后进入 unmeasurable，理由为 unreliable_exit_quote。"""
    now = datetime.now(timezone.utc)
    t_analysis = now - timedelta(hours=25.0)
    _setup_event_and_snapshots(
        db,
        event_id="EVT-STALE-1",
        security_id="SEC-US-MU",
        direction="bullish",
        event_time=t_analysis,
        analysis_time=t_analysis,
        anchor_price=100.0,
        event_version=1,
    )
    # 报价的 market_timestamp 停留在 25 小时前（stale），但在 horizon (24h) + max_delay (24h) = 48h 之内
    stale_confirmer = _MultiConfirmer({"MU": 105.0}, as_of=now, market_ts=t_analysis)
    ledger = ForecastLedger(db, config, stale_confirmer)

    # 未超最大延迟时保持 pending
    checks_pending = ledger.run_due_checks(now=now)
    assert len(checks_pending) == 0
    snap = ForecastSnapshotRepo(db).get("SNAP-EVT-STALE-1-v1")
    assert snap.status == "pending"

    # 推进时间至超过最大允许延迟 (elapsed > 48h，例如 50h)
    way_past_now = t_analysis + timedelta(hours=50.0)
    stale_confirmer_late = _MultiConfirmer({"MU": 105.0}, as_of=way_past_now, market_ts=t_analysis)
    ledger_late = ForecastLedger(db, config, stale_confirmer_late)
    checks_unmeasurable = ledger_late.run_due_checks(now=way_past_now)
    assert len(checks_unmeasurable) == 1
    assert checks_unmeasurable[0].outcome == "unmeasurable"
    assert checks_unmeasurable[0].note == "unreliable_exit_quote"
    snap_after = ForecastSnapshotRepo(db).get("SNAP-EVT-STALE-1-v1")
    assert snap_after.status == "unmeasurable"

