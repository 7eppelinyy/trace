"""预测回测账本测试：方向预测 vs 事后真实行情的确定性核对。

原则：
    - 核对规则确定性（阈值映射方向），不走 LLM；
    - 行情缺失不落账（保持 pending，不得伪造核对结果）；
    - 每个 impact 只核对一次（幂等）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trace.collectors.market_data.base import Quote
from trace.domain.models import Event, EventImpact
from trace.db.repositories import EventImpactRepo, EventRepo
from trace.feedback.ledger import ForecastLedger


class _FakeConfirmer:
    """quote 返回 last=100±change% 的假行情（change=None 模拟无数据）。"""

    def __init__(self, change_pct: float | None):
        self.change_pct = change_pct
        self.calls: list[tuple[str, str]] = []

    def quote(self, market: str, ticker: str) -> Quote | None:
        self.calls.append((market, ticker))
        if self.change_pct is None:
            return None
        prev = 100.0
        last = prev * (1 + self.change_pct / 100)
        return Quote(ticker=ticker, ts=datetime.now(timezone.utc),
                     last_price=round(last, 4), prev_close=prev)


def _seed_impact(db, *, direction="bullish", age_hours=30.0,
                 impact_id="IMP-1", event_id="e1") -> None:
    now = datetime.now(timezone.utc)
    EventRepo(db).insert(Event(
        event_id=event_id, title="Micron raises DRAM prices", version=1,
        event_time=now - timedelta(hours=age_hours),
        first_seen_at=now - timedelta(hours=age_hours)))
    EventImpactRepo(db).upsert(EventImpact(
        impact_id=impact_id, event_id=event_id,
        security_id="SEC-US-MU", direction=direction,
        confidence=0.7, final_score=8.0))


def _ledger(db, config, confirmer) -> ForecastLedger:
    return ForecastLedger(db, config, confirmer)


def test_hit_when_direction_matches(db, config):
    _seed_impact(db, direction="bullish")
    confirmer = _FakeConfirmer(+2.5)
    recorded = _ledger(db, config, confirmer).run_due_checks()
    assert len(recorded) == 1
    assert recorded[0].outcome == "hit"
    assert recorded[0].actual_direction == "bullish"


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


def test_no_quote_keeps_pending(db, config):
    """行情缺失：不落账、不伪造结果，下轮重试。"""
    _seed_impact(db)
    ledger = _ledger(db, config, _FakeConfirmer(None))
    assert ledger.run_due_checks() == []
    s = ledger.summary()
    assert s["total"] == 0 and s["pending"] == 1


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
    assert "方向预测 ≠ 涨跌建议" in text
