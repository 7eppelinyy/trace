"""LLM 每日调用预算原子竞争与并发安全测试。"""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
import pytest

from trace.ai.budget import LLMBudget, LLMBudgetExceededError
from trace.ai.llm_client import LLMClient
from trace.config import LLMConfig
from trace.db.connection import Database
from trace.db.migration import apply_migrations


class _DummyProvider:
    def __init__(self):
        self.call_count = 0

    def generate(self, model, system_prompt, user_prompt, schema=None):
        self.call_count += 1
        return '{"result": "ok"}'

    def generate_text(self, model, system_prompt, user_prompt="", messages=None, max_tokens=2000, temperature=None):
        self.call_count += 1
        return "Dummy text response"


def test_budget_atomic_try_consume_concurrency(tmp_path):
    """测试 20 个并发线程竞争 1 次预算时，严格仅 1 个成功扣减。"""
    db_path = tmp_path / "budget_test.db"
    db = Database(db_path)
    apply_migrations(db)

    budget = LLMBudget(db, daily_limit=1)

    success_count = 0
    fail_count = 0

    def attempt_consume():
        # 每个线程使用自己的 Database 连接，测试真正的 SQLite 多连接并发
        thread_db = Database(db_path)
        thread_budget = LLMBudget(thread_db, daily_limit=1)
        res = thread_budget.try_consume(1)
        thread_db.close()
        return res

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(attempt_consume) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            if f.result():
                success_count += 1
            else:
                fail_count += 1

    assert success_count == 1, f"Expected exactly 1 success, got {success_count}"
    assert fail_count == 19, f"Expected 19 failures, got {fail_count}"

    # 验证数据库中记录的调用次数严格为 1
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = db.query_one("SELECT calls FROM llm_usage WHERE date_str=?", (today,))
    assert row is not None
    assert int(row["calls"]) == 1
    db.close()


def test_llm_client_complete_text_concurrency_with_budget(tmp_path):
    """测试 LLMClient 在 20 线程并发时通过 atomic reserve 保护，Provider 仅被真实调用 1 次。"""
    db_path = tmp_path / "client_budget_test.db"
    db = Database(db_path)
    apply_migrations(db)

    budget = LLMBudget(db, daily_limit=1)
    cfg = LLMConfig(provider="gemini", max_retries=0)
    provider = _DummyProvider()

    client = LLMClient(llm_config=cfg, budget=budget)
    client._provider = provider

    successful_results = []
    budget_errors = []
    other_errors = []

    def call_client():
        try:
            res = client.complete_text(
                model="test-model",
                system_prompt="sys",
                user_prompt="user",
            )
            successful_results.append(res)
        except LLMBudgetExceededError as exc:
            budget_errors.append(exc)
        except Exception as exc:
            other_errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(call_client) for _ in range(20)]
        concurrent.futures.wait(futures)

    assert len(successful_results) == 1, f"Expected 1 success, got {len(successful_results)}"
    assert len(budget_errors) == 19, f"Expected 19 LLMBudgetExceededError, got {len(budget_errors)}"
    assert provider.call_count == 1, f"Provider should only be called once, got {provider.call_count}"

    db.close()


def test_budget_boundary_and_validation(tmp_path):
    """测试负数与零预约被安全放行不增量，跨日与禁用状态正确。"""
    db_path = tmp_path / "budget_bound.db"
    db = Database(db_path)
    apply_migrations(db)

    # 1) 未启用预算（daily_limit <= 0）
    disabled_budget = LLMBudget(db, daily_limit=0)
    assert not disabled_budget.enabled
    assert disabled_budget.try_consume(5)
    assert disabled_budget.used() == 5

    # 2) 启用预算后非法 n (n <= 0) 不扣减
    active_budget = LLMBudget(db, daily_limit=10)
    import pytest
    for invalid in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="positive integer"):
            active_budget.try_consume(invalid)
    assert active_budget.used() == 5

    # 3) 正常消费达到上限
    assert active_budget.try_consume(5)
    assert active_budget.used() == 10
    # 再次消费被拒绝
    assert not active_budget.try_consume(1)
    assert active_budget.used() == 10

    db.close()


def test_budget_multiprocess_competition(tmp_path):
    """真正使用两个独立子进程竞争同一 SQLite 文件数据库的调用预算。"""
    import subprocess
    import sys

    db_path = tmp_path / "proc_budget.db"
    db = Database(db_path)
    apply_migrations(db)
    db.close()

    daily_limit = 5
    attempts_per_process = 10

    # Helper script for subprocess
    script = f"""
import sys
from trace.db.connection import Database
from trace.ai.budget import LLMBudget

db_path = sys.argv[1]
daily_limit = int(sys.argv[2])
attempts = int(sys.argv[3])

db = Database(db_path)
budget = LLMBudget(db, daily_limit=daily_limit)

successes = 0
for _ in range(attempts):
    try:
        if budget.try_consume(1):
            successes += 1
    except Exception:
        pass
db.close()
print(successes)
"""
    helper_path = tmp_path / "proc_runner.py"
    helper_path.write_text(script, encoding="utf-8")

    import os
    from pathlib import Path
    repo_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=repo_root)

    # Launch two subprocesses concurrently
    p1 = subprocess.Popen(
        [sys.executable, str(helper_path), str(db_path), str(daily_limit), str(attempts_per_process)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repo_root,
        env=env,
    )
    p2 = subprocess.Popen(
        [sys.executable, str(helper_path), str(db_path), str(daily_limit), str(attempts_per_process)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repo_root,
        env=env,
    )

    out1, err1 = p1.communicate(timeout=15)
    out2, err2 = p2.communicate(timeout=15)

    assert p1.returncode == 0, f"Process 1 failed: {err1}"
    assert p2.returncode == 0, f"Process 2 failed: {err2}"

    s1 = int(out1.strip())
    s2 = int(out2.strip())

    assert s1 + s2 == daily_limit, f"Total successful consumes {s1 + s2} must equal daily_limit {daily_limit}"

    # Verify database state
    db_verify = Database(db_path)
    budget_verify = LLMBudget(db_verify, daily_limit=daily_limit)
    assert budget_verify.used() == daily_limit
    assert budget_verify.remaining() == 0
    db_verify.close()


def test_budget_utc_rollover(tmp_path, monkeypatch):
    """验证 UTC 日切：前一天的用量耗尽不阻断新一天的预算配额，且前一天历史记录保持可查。"""
    db_path = tmp_path / "rollover.db"
    db = Database(db_path)
    apply_migrations(db)

    budget = LLMBudget(db, daily_limit=3)

    # 1. 模拟 UTC 日期为 2026-09-20
    monkeypatch.setattr(LLMBudget, "_today", staticmethod(lambda: "2026-09-20"))
    assert budget.try_consume(3)
    assert budget.used() == 3
    assert budget.remaining() == 0
    # 超额被拒
    assert not budget.try_consume(1)

    # 2. 推进到新一天 2026-09-21
    monkeypatch.setattr(LLMBudget, "_today", staticmethod(lambda: "2026-09-21"))
    assert budget.used() == 0
    assert budget.remaining() == 3
    assert budget.try_consume(2)
    assert budget.used() == 2
    assert budget.remaining() == 1

    # 3. 验证历史日期的计数仍保持可查
    assert budget.used("2026-09-20") == 3
    assert budget.used("2026-09-21") == 2

    db.close()


def test_budget_ask_user_and_reserve(tmp_path):
    """测试 Ask 用户预算、全局预算与后台保留额度的隔离控制。"""
    db_path = tmp_path / "ask_budget.db"
    db = Database(db_path)
    apply_migrations(db)

    # daily_limit=10, ask_limit=8, ask_user_limit=3, pipeline_reserve=4
    # 允许的交互 Ask 总次数上限为: 10 - 4 = 6 次
    budget = LLMBudget(db, daily_limit=10, ask_limit=8, ask_user_limit=3, pipeline_reserve=4)

    # 1. Ask 未传 user_id 必须抛异常
    with pytest.raises(ValueError, match="authenticated user"):
        budget.try_consume(1, usage_type="ask", user_id=None)

    # 2. 用户 user_A 连续调用 3 次成功
    assert budget.try_consume(1, usage_type="ask", user_id="user_A")
    assert budget.try_consume(1, usage_type="ask", user_id="user_A")
    assert budget.try_consume(1, usage_type="ask", user_id="user_A")
    # user_A 达到单用户上限 (3次)，第 4 次被拒
    assert not budget.try_consume(1, usage_type="ask", user_id="user_A")

    # 3. 用户 user_B 调用不受 user_A 影响，可继续消费
    assert budget.try_consume(1, usage_type="ask", user_id="user_B")
    assert budget.try_consume(1, usage_type="ask", user_id="user_B")
    assert budget.try_consume(1, usage_type="ask", user_id="user_B")
    # 当前已累计 6 次 (3 + 3)，达到交互上限 10 - 4 = 6

    # 4. 新用户 user_C 发起 Ask 调用：被 pipeline_reserve 拦截
    assert not budget.try_consume(1, usage_type="ask", user_id="user_C")

    # 5. 后台 pipeline 任务调用不受交互上限限制，可继续消耗剩余的保留额度 (至 10)
    assert budget.try_consume(1, usage_type="pipeline")
    assert budget.try_consume(1, usage_type="pipeline")
    assert budget.try_consume(1, usage_type="pipeline")
    assert budget.try_consume(1, usage_type="pipeline")
    assert budget.used() == 10
    # 总额度 10 耗尽，pipeline 也无法再消费
    assert not budget.try_consume(1, usage_type="pipeline")

    db.close()


def test_complete_text_with_ask_user_quota(tmp_path):
    """测试 complete_text 支持 usage_type='ask' 并受用户配额约束，不能绕过预算层。"""
    db_path = tmp_path / "complete_text_ask.db"
    db = Database(db_path)
    apply_migrations(db)

    budget = LLMBudget(db, daily_limit=10, ask_limit=5, ask_user_limit=2, pipeline_reserve=2)
    cfg = LLMConfig(provider="gemini", max_retries=0)
    provider = _DummyProvider()

    client = LLMClient(llm_config=cfg, budget=budget)
    client._provider = provider

    # 1. 正常使用 user_1 消费两次
    res1 = client.complete_text("m", "sys", "prompt1", usage_type="ask", user_id="user_1")
    assert res1 == "Dummy text response"
    res2 = client.complete_text("m", "sys", "prompt2", usage_type="ask", user_id="user_1")
    assert res2 == "Dummy text response"

    # 2. 第三次消费抛 LLMBudgetExceededError
    with pytest.raises(LLMBudgetExceededError):
        client.complete_text("m", "sys", "prompt3", usage_type="ask", user_id="user_1")

    # 3. 另一个用户 user_2 仍可调用
    res3 = client.complete_text("m", "sys", "prompt1", usage_type="ask", user_id="user_2")
    assert res3 == "Dummy text response"

    db.close()
