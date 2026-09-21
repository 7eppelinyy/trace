# N01 测试时钟修复与基线恢复报告

- **日期**：2026-09-21
- **执行环境**：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11.15), Node v24.16.0
- **状态**：PASS

## 1. 问题定位与根因
- 失败测试：`tests/test_immutable_forecast_snapshots.py::test_immutable_snapshots_on_revision`
- 根因分析：
  测试中将预测修订后的评估时刻推进到 `future_now = t_v2 + 25h`，但测试假行情 `_MultiConfirmer.quote()` 始终使用未参数化的 `datetime.now(timezone.utc)` 生成市场报价时间戳。
  系统新加入的报价时效门禁 `quote_quality(quote, now)` 准确检测到该报价时间戳滞后于评估时钟超过 300 秒，正确将其判定为 `stale`。由于 `elapsed_hours` 未超过最大容忍延迟（`horizon_hours + max_delay_hours = 48h`），账本按契约保持 `pending` 等待新鲜行情，导致测试断言核对记录数为 1 失败（返回 0）。

## 2. 修复方案与工程实现
1. **显式时钟注入**：
   - 为 `_MultiConfirmer` 增加 `as_of: datetime | None = None` 与 `market_ts: datetime | None = None` 参数。
   - `_MultiConfirmer.quote()` 生成的 `Quote` 严格对齐注入的 `as_of` 时钟，保证测试内的行情时钟与评估时钟一致。
2. **测试时钟对齐**：
   - 初次核对注入 `as_of=now`；
   - 修订核对注入 `as_of=future_now`。
3. **增加反向负例** (`test_stale_exit_quote_handling`)：
   - 刻意将 `market_timestamp` 留在历史时间（滞后 25 小时）；
   - 验证在未超过 `horizon + max_delay`（48小时）时，`run_due_checks` 保持 `pending`（不制造无效结果）；
   - 推进时钟至 50 小时（超期）后，验证其正确落账为 `unmeasurable`，理由为 `unreliable_exit_quote`，快照状态标记为 `unmeasurable`。

## 3. 验证结果
- `tests/test_immutable_forecast_snapshots.py`：6 passed in 2.04s
- 预测与行情专项：40 passed in 9.51s
- Node 测试：
  - `miniprogram/tests/detail_mapping.test.js`: 7 passed
  - `miniprogram/tests/session_research.test.js`: All contracts passed
- Python 全量套件：**408 passed, 1 warning in 62.69s**
