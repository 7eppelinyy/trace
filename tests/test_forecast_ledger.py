"""预测回测账本测试：方向预测 vs 事后真实行情的确定性核对。

原则：
    - 收益必须是**事件锚定的区间收益**（事件时刻锚点价 → 核对时刻价格），
      不是核对时刻的当日涨跌；
    - 核对规则确定性（阈值映射方向），不走 LLM；
    - 行情缺失不落账（保持 pending，不得伪造核对结果）；
    - 拿不到锚点价 / 核对严重超时 → unmeasurable，落账但不计入命中率
      （否则这些 impact 会永远停在 pending）；
    - 每个 impact 只核对一次（幂等）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trace.collectors.market_data.base import Quote
from trace.domain.models import Event, EventImpact, MarketSnapshot
from trace.db.repositories import EventImpactRepo, EventRepo, MarketSnapshotRepo
from trace.feedback.ledger import ForecastLedger

ANCHOR_PRICE = 100.0


class _FakeConfirmer:
    """核对时刻的假行情。

    last_price 由 change_pct 相对锚点价算出；prev_close 故意设成另一个值，
    这样"用锚点算区间收益"和"用 change_pct_from_prev 算当日涨跌"会得到
    不同答案 —— 测试才能真正锁住口径。
    """

    def __init__(self, change_pct: float | None, prev_close: float | None = None):
        self.change_pct = change_pct
        self.prev_close = prev_close
        self.calls: list[tuple[str, str]] = []

    def quote(self, market: str, ticker: str, *,
              security_id: str | None = None) -> Quote | None:
        self.calls.append((market, ticker))
        if self.change_pct is None:
            return None
        last = ANCHOR_PRICE * (1 + self.change_pct / 100)
        return Quote(ticker=ticker, ts=datetime.now(timezone.utc),
                     last_price=round(last, 4),
                     prev_close=self.prev_close if self.prev_close is not None else last)


def _seed_impact(db, *, direction="bullish", age_hours=30.0,
                 impact_id="IMP-1", event_id="e1",
                 anchor_price: float | None = ANCHOR_PRICE,
                 anchor_offset_hours: float = 0.0) -> datetime:
    """建事件 + 影响预测，并在事件时刻附近落一条行情快照作为锚点。

    anchor_price=None 表示不落快照（模拟事件早于快照采集的历史数据）。
    """
    now = datetime.now(timezone.utc)
    event_time = now - timedelta(hours=age_hours)
    EventRepo(db).insert(Event(
        event_id=event_id, title="Micron raises DRAM prices", version=1,
        event_time=event_time, first_seen_at=event_time))
    EventImpactRepo(db).upsert(EventImpact(
        impact_id=impact_id, event_id=event_id,
        security_id="SEC-US-MU", direction=direction,
        confidence=0.7, final_score=8.0))
    if anchor_price is not None:
        MarketSnapshotRepo(db).insert(MarketSnapshot(
            security_id="SEC-US-MU",
            ts=event_time + timedelta(hours=anchor_offset_hours),
            last_price=anchor_price, prev_close=anchor_price))
    return event_time


def _ledger(db, config, confirmer) -> ForecastLedger:
    return ForecastLedger(db, config, confirmer)


# ---------------------------------------------------------------------------
# 方向核对
# ---------------------------------------------------------------------------

def test_hit_when_direction_matches(db, config):
    _seed_impact(db, direction="bullish")
    recorded = _ledger(db, config, _FakeConfirmer(+2.5)).run_due_checks()
    assert len(recorded) == 1
    assert recorded[0].outcome == "hit"
    assert recorded[0].actual_direction == "bullish"
    assert recorded[0].actual_change_pct == pytest.approx(2.5, abs=1e-3)


def test_miss_when_direction_opposes(db, config):
    _seed_impact(db, direction="bullish")
    recorded = _ledger(db, config, _FakeConfirmer(-3.0)).run_due_checks()
    assert recorded[0].outcome == "miss"
    assert recorded[0].actual_direction == "bearish"


def test_neutral_when_move_below_threshold(db, config):
    _seed_impact(db, direction="bearish")
    recorded = _ledger(db, config, _FakeConfirmer(+0.2)).run_due_checks()
    assert recorded[0].outcome == "neutral"
    assert recorded[0].actual_direction == "neutral"


def test_return_is_event_anchored_not_intraday(db, config):
    """核心口径回归：必须用事件锚点价算区间收益，不能用当日涨跌。

    锚点 100 → 核对时 103（区间 +3%，看涨命中）；
    但该报价的 prev_close 是 104，当日涨跌为 -0.96%（会被判成 neutral）。
    早期实现用的正是后者。
    """
    _seed_impact(db, direction="bullish")
    recorded = _ledger(db, config,
                       _FakeConfirmer(+3.0, prev_close=104.0)).run_due_checks()

    assert recorded[0].actual_change_pct == pytest.approx(3.0, abs=1e-3)
    assert recorded[0].outcome == "hit"
    assert recorded[0].anchor_price == pytest.approx(100.0)
    assert recorded[0].exit_price == pytest.approx(103.0)


def test_check_records_audit_fields(db, config):
    """每条核对都要能复算：锚点价/锚点时刻/到期价/真实间隔。"""
    _seed_impact(db, direction="bullish", age_hours=30.0)
    check = _ledger(db, config, _FakeConfirmer(+2.0)).run_due_checks()[0]
    assert check.anchor_price == pytest.approx(ANCHOR_PRICE)
    assert check.anchor_ts is not None
    assert check.exit_price is not None
    assert check.elapsed_hours == pytest.approx(30.0, abs=0.2)
    assert check.horizon_hours == 24.0


# ---------------------------------------------------------------------------
# 行情缺失 / 无法核对
# ---------------------------------------------------------------------------

def test_no_quote_keeps_pending(db, config):
    """到期行情缺失：不落账、不伪造结果，下轮重试。"""
    _seed_impact(db, age_hours=30.0)
    ledger = _ledger(db, config, _FakeConfirmer(None))
    assert ledger.run_due_checks() == []
    s = ledger.summary()
    assert s["total"] == 0 and s["pending"] == 1


def test_missing_anchor_is_unmeasurable_not_pending(db, config):
    """事件早于快照采集：锚点永远补不回来，落账为 unmeasurable。

    留在 pending 会让"待核对"变成只增不减的垃圾桶。
    """
    _seed_impact(db, anchor_price=None)
    ledger = _ledger(db, config, _FakeConfirmer(+2.0))
    recorded = ledger.run_due_checks()

    assert len(recorded) == 1
    assert recorded[0].outcome == "unmeasurable"
    assert recorded[0].note == "no_anchor_snapshot"
    s = ledger.summary()
    assert s["unmeasurable"] == 1
    assert s["total"] == 0                  # 不计入已核对
    assert s["hit_rate"] is None            # 不污染命中率
    assert s["pending"] == 0                # 已落账，不再重试


def test_anchor_outside_tolerance_is_unmeasurable(db, config):
    """快照离事件太远：不得用别的时刻的价格顶替事件时刻的价格。"""
    _seed_impact(db, anchor_offset_hours=-12.0)   # 容差默认 6h
    recorded = _ledger(db, config, _FakeConfirmer(+2.0)).run_due_checks()
    assert recorded[0].outcome == "unmeasurable"
    assert recorded[0].note == "no_anchor_snapshot"


def test_severely_delayed_check_is_unmeasurable(db, config):
    """服务停机数日后补跑：测到的已不是 horizon 收益，不得当作有效样本。"""
    _seed_impact(db, age_hours=120.0)             # horizon 24h + 宽限 24h
    recorded = _ledger(db, config, _FakeConfirmer(+5.0)).run_due_checks()
    assert recorded[0].outcome == "unmeasurable"
    assert recorded[0].note == "check_delayed"
    assert recorded[0].elapsed_hours == pytest.approx(120.0, abs=0.2)


# ---------------------------------------------------------------------------
# 调度与幂等
# ---------------------------------------------------------------------------

def test_not_due_within_horizon(db, config):
    _seed_impact(db, age_hours=1.0)
    confirmer = _FakeConfirmer(+2.0)
    assert _ledger(db, config, confirmer).run_due_checks() == []
    assert confirmer.calls == []            # 未到期连行情都不该请求


def test_idempotent_one_check_per_impact(db, config):
    _seed_impact(db)
    confirmer = _FakeConfirmer(+2.0)
    ledger = _ledger(db, config, confirmer)
    assert len(ledger.run_due_checks()) == 1
    assert ledger.run_due_checks() == []    # 第二轮：已落账，不再核对
    assert len(confirmer.calls) == 1


def test_uncertain_directions_not_checked(db, config):
    _seed_impact(db, direction="uncertain")
    ledger = _ledger(db, config, _FakeConfirmer(+2.0))
    assert ledger.run_due_checks() == []
    assert ledger.summary()["pending"] == 0


def test_summary_and_render(db, config):
    _seed_impact(db, direction="bullish", impact_id="IMP-1")
    _seed_impact(db, direction="bearish", impact_id="IMP-2", event_id="e2")
    ledger = _ledger(db, config, _FakeConfirmer(+2.0))
    ledger.run_due_checks()
    s = ledger.summary()
    # 实际 +2%：bullish 预测命中，bearish 预测反向未命中
    assert s["total"] == 2 and s["hits"] == 1 and s["misses"] == 1
    assert s["hit_rate"] == 0.5
    text = ledger.render_summary()
    assert "命中率: 50%" in text
    assert "区间收益" in text               # 口径必须写在给用户看的文案里
