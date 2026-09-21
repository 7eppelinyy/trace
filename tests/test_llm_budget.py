"""LLM 每日调用预算测试（成本熔断）。

失控场景是静默的：来源改版 → canonical_url / source_item_id 变了 →
Level 1 确定性去重全线穿透 → 同一批新闻每轮都被当成新条目重跑 Stage A/B。
功能测试全绿、日志正常，只有账单会变。

因此这里锁三件事：
    - 计数落库（进程重启后仍然有效，内存计数器等于没有护栏）
    - 重试也算数（失控开销大部分来自重试）
    - 超限必须 STOP，**不得**静默降级成 rule_based_degraded 伪装完整分析
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trace.ai.budget import LLMBudget, LLMBudgetExceededError
from trace.ai.llm_client import LLMClient
from trace.ai.schemas import LLMUnavailableError
from trace.common.modes import STOP_LLM_BUDGET_EXCEEDED
from trace.common.ids import raw_item_id
from trace.domain.models import RawItem
from trace.pipeline import Pipeline
from trace.collectors.base import CollectResult


class _Config:
    """最小 LLMConfig 桩（够 LLMClient 用）。"""
    provider = "gemini"
    api_key = "k"
    base_url = ""
    temperature = 0.0
    timeout_seconds = 5
    max_retries = 2
    enabled = True


# ---------------------------------------------------------------------------
# 计数与上限
# ---------------------------------------------------------------------------

def test_budget_counts_persist_across_instances(db):
    """计数落库：长驻服务重启、run-once 新进程都必须接着上次的数继续算。"""
    LLMBudget(db, daily_limit=10).consume(3)
    assert LLMBudget(db, daily_limit=10).used() == 3      # 新实例读到同一天的累计
    LLMBudget(db, daily_limit=10).consume(2)
    assert LLMBudget(db, daily_limit=10).remaining() == 5


def test_budget_check_raises_when_exhausted(db):
    budget = LLMBudget(db, daily_limit=2)
    budget.check()                                        # 0/2 放行
    budget.consume(2)
    with pytest.raises(LLMBudgetExceededError) as exc:
        budget.check()
    assert "2/2" in str(exc.value)


def test_budget_disabled_never_blocks_but_still_counts(db):
    """daily_limit=0：不熔断，但照样计数 —— 否则没法给上限选合适的值。"""
    budget = LLMBudget(db, daily_limit=0)
    budget.consume(9999)
    budget.check()                                        # 不抛
    assert budget.used() == 9999
    assert budget.remaining() is None
    assert "未设上限" in budget.render()


def test_budget_is_scoped_to_utc_day(db):
    budget = LLMBudget(db, daily_limit=5)
    budget.consume(4)
    yesterday = "2000-01-01"
    db.execute("INSERT INTO llm_usage (date_str, calls) VALUES (?, ?)",
               (yesterday, 999))
    assert budget.used() == 4                             # 昨天的用量不占今天额度
    assert budget.used(yesterday) == 999


def test_budget_exceeded_is_an_llm_unavailable_error():
    """刻意继承关系：Stage A/B 对 LLMUnavailableError 是"向上抛不降级"，
    对普通 Exception 是"落规则兜底"。预算耗尽必须走前者。"""
    assert issubclass(LLMBudgetExceededError, LLMUnavailableError)


# ---------------------------------------------------------------------------
# LLMClient 集成：重试也消耗预算
# ---------------------------------------------------------------------------

def _client(db, limit: int, generate):
    client = LLMClient(_Config(), backoff_base=0,
                       budget=LLMBudget(db, daily_limit=limit))

    class _Provider:
        name = "stub"

        def generate(self, model, system_prompt, user_prompt, schema=None):
            return generate()

    client._provider = _Provider()
    return client


def test_successful_call_consumes_one(db):
    client = _client(db, 10, lambda: '{"ok": true}')
    client.complete_json("m", "sys", "user")
    assert client.budget.used() == 1


def test_retries_consume_budget(db):
    """max_retries=2 → 最多 3 次真实请求，每次都要计入预算。"""
    def boom():
        raise RuntimeError("upstream 500")

    client = _client(db, 10, boom)
    with pytest.raises(RuntimeError):
        client.complete_json("m", "sys", "user")
    assert client.budget.used() == 3


def test_client_stops_at_budget_limit(db):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return '{"ok": true}'

    client = _client(db, 2, counting)
    client.complete_json("m", "sys", "user")
    client.complete_json("m", "sys", "user")
    with pytest.raises(LLMBudgetExceededError):
        client.complete_json("m", "sys", "user")
    assert calls["n"] == 2, "超限后不得再打真实请求"


# ---------------------------------------------------------------------------
# 流水线：超限 STOP，不得伪装
# ---------------------------------------------------------------------------

def test_pipeline_stops_on_budget_exhaustion(app, monkeypatch):
    """预算耗尽：整轮返回 STOP_LLM_BUDGET_EXCEEDED，不产出任何降级分析。"""
    app.db.execute("UPDATE source SET can_store=1 WHERE source_id='src_reuters'")
    pipeline = Pipeline(app)
    now = datetime.now(timezone.utc)
    item = RawItem(raw_item_id=raw_item_id(), source_id="src_reuters",
                   source_item_id="B1", title="Micron 扩产", url="https://b.test/1",
                   published_at=now, language="zh", content="Micron 扩产")
    monkeypatch.setattr(app.collectors, "run_all",
                        lambda **kw: ([item], [CollectResult(source_ids=["src_reuters"])]))

    def exhausted(_item):
        raise LLMBudgetExceededError("daily LLM call budget exhausted: 3000/3000")

    monkeypatch.setattr(app.pipeline.extractor, "extract", exhausted)

    summary = pipeline.run_once()

    assert summary.status == STOP_LLM_BUDGET_EXCEEDED
    assert any("budget" in n for n in summary.notes)
    assert summary.events_created == 0
    assert summary.events_analyzed == 0
    # 关键：不得留下任何"看起来分析过了"的记录
    assert app.db.query("SELECT * FROM event_impact") == []


def test_budget_stop_is_distinct_from_missing_key(app, monkeypatch):
    """预算耗尽与缺 Key 是两回事，状态码不得混用（排障口径）。"""
    app.db.execute("UPDATE source SET can_store=1 WHERE source_id='src_reuters'")
    pipeline = Pipeline(app)
    now = datetime.now(timezone.utc)
    item = RawItem(raw_item_id=raw_item_id(), source_id="src_reuters",
                   source_item_id="B2", title="t", url="https://b.test/2",
                   published_at=now, language="zh", content="t")
    monkeypatch.setattr(app.collectors, "run_all",
                        lambda **kw: ([item], [CollectResult(source_ids=["src_reuters"])]))
    monkeypatch.setattr(app.pipeline.extractor, "extract",
                        lambda _i: (_ for _ in ()).throw(
                            LLMUnavailableError("no api key")))

    summary = pipeline.run_once()
    assert summary.status != STOP_LLM_BUDGET_EXCEEDED
