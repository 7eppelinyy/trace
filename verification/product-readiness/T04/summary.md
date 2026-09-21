# 任务 T04 交付与验收报告：原子预算、限流与调用账本

- **任务 ID**: T04
- **优先级**: P0
- **审阅对应**: F04
- **执行时间**: 2026-09-19 (Asia/Taipei)
- **状态**: 已完成并通过自动化并发验收

---

## 1. 问题与修复方案

| 维度 | 修复前实际行为 (Bug) | 修复后实际行为 (Fixed) | 验证证据 |
|---|---|---|---|
| **调用检查与扣减原子性** | `complete_json` 与 `complete_text` 走 `check()` 再 `consume()` 模式，并发竞争时可同时绕过上限导致超额调用 | `try_consume(n)` 统一作为调用前原子预约入口，使用 `BEGIN IMMEDIATE` 短事务获取排他写锁，检查与扣减在一个原子决策内完成 | 20 线程并发竞争测试仅 1 次成功，其余 19 次准确抛出 `LLMBudgetExceededError` |
| **Provider 真实调用保护** | 并发穿透时 Provider 会被多次真实调用，产生未经授权的高额账单 | 当原子预约返回 False 时直接在上层拦截，不进入 Provider 的真实网络调用 | `provider.call_count == 1`（测试断言严格成立） |
| **异常类型化** | `ask.py` 曾使用脆弱的字符串包含 `"budget exhausted" in str(exc).lower()` | 全面使用类型化异常 `LLMBudgetExceededError`，精确捕获并返回 `status: "budget_exhausted"` | `tests/test_budget_concurrency.py` 验证通过 |
| **并发写锁冲突 (SQLITE_BUSY)** | 默认 `BEGIN DEFERRED` 读转写可能导致并发死锁抛错 | `Database.transaction(mode="IMMEDIATE")` 结合已配置的 WAL 模式与 15s `busy_timeout`，保障多连接并发平滑排队 | 20 个独立线程独立数据库连接并发测试通过 |
| **问答模型配置兼容 (F08)** | `ask.py` 硬编码 `model_name = getattr(self.llm.config, "model", "deepseek-chat")`，当配置其他 provider/模型时易错配 | 支持 `model_ask` 配置项，优先使用明确指定的问答模型，兼顾 Provider 兼容性 | 代码审查与集成测试通过 |

---

## 2. 自动化测试结果

执行命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_budget_concurrency.py -v
```

输出：
```text
tests/test_budget_concurrency.py::test_budget_atomic_try_consume_concurrency PASSED [ 33%]
tests/test_budget_concurrency.py::test_llm_client_complete_text_concurrency_with_budget PASSED [ 66%]
tests/test_budget_concurrency.py::test_budget_boundary_and_validation PASSED [100%]

============================== 3 passed in 1.01s ==============================
```

---

## 3. 回滚方案
如需临时回滚，可通过配置 `llm.daily_call_budget: 0` 暂时解除上限，但严禁恢复“先 check 后 consume”的非原子调用路径。
