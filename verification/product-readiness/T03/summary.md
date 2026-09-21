# T03 · 持久化处理作业与人审恢复 验证报告

## 1. 任务概述
- **任务编号**: T03 (P0)
- **关联问题**: F03（处理作业未持久化与恢复脆弱）、F11（人审记录悬空与原文丢失）、Probe 11.2
- **核心目标**: 引入 `processing_job` 状态追踪机制，确保人审记录具有真实持久化的 `raw_item` 支持；实现 Stage B 预算熔断中断后在零新条目轮次下的可靠无损自动恢复；短事务保护多表写入；提供遗留孤立数据修复扫描。

---

## 2. 核心架构与代码变更

### 2.1 数据库迁移与模型
- **SQL 迁移**: `trace/db/migrations/0015_processing_job.sql`
  - 新增 `processing_job` 表：包含 `job_id`, `job_type`, `target_id`, `status`, `processor_version`, `input_version`, `lease_until`, `retry_count`, `max_retries`, `last_error`, `created_at`, `updated_at`。
- **领域模型与仓库**: `trace/domain/models.py` (`ProcessingJob`) 与 `trace/db/repositories.py` (`ProcessingJobRepo`)。
  - 支持 `create_or_update`, `mark_status`, `list_pending`, `get_by_target`。

### 2.2 人审原文与 RawItem 持久化保证 (`trace/pipeline.py` & `trace/db/health.py`)
- 在 Stage A 触发 `SchemaValidationError` 时，强制先调用 `RawItemRepo(ctx.db).insert(item)` 将原始采集条目落库，再写入 `HumanReviewRepo`。
- 新增 `HumanReviewRepo.get_with_raw(review_id)` 穿透查询，支持管理台与 CLI 完整检视原始标题、正文、发布时间与数据源。
- 彻底解决人审记录中 `r.raw_item_id IS NULL` 的原文丢失问题（Probe 11.2 人审用例通过）。

### 2.3 Stage B 跨轮次中断自动恢复机制 (`trace/pipeline.py`)
- 当 Stage B 遭遇 `LLMBudgetExceededError` 预算熔断时，对应作业记录为 `status='blocked_budget'`，游标已正常提交。
- 流水线在后续轮次启动时（`_run_once`），主动通过 `ProcessingJobRepo.list_pending("stage_b_analyze")` 捞取遗留未完成作业。
- 即使因游标提交导致采集器返回 0 条新条目，遗留事件仍自动进入 Stage B 补跑分析。
- 当分析结果产生合法空影响（0 impacts）时，作业状态被更新为 `succeeded_empty`，杜绝死循环反复消费分析配额。

### 2.4 多表写入短事务与原子性 (`trace/event_engine/engine.py`)
- `_create_event` 与 `_merge_into` 中的 `raw_repo`, `event_repo`, `source_repo`, `reviser` 写入均由 `with self.db.transaction():` 显式包裹，嵌入向量运算（CPU密集型）置于事务外，保证锁占用时间极短且状态原子提交。

### 2.5 遗留孤立数据一致性扫描器 (`trace/event_engine/repair.py`)
- 提供 `scan_and_repair_inconsistencies(db, dry_run=True/False)`：
  - 扫描无分析事件并补充 Stage B 待办作业；
  - 扫描无追踪 raw 条目并补全状态；
  - 扫描缺失原文的悬空人审记录，显式标记为 `legacy_payload_missing`，严禁虚构伪造。

---

## 3. 测试验证矩阵

### 3.1 专用测试集 `tests/test_processing_recovery.py` (4/4 Passed)
| 用例编号 | 验证场景 | 预期行为 | 测试结果 |
| :--- | :--- | :--- | :--- |
| **PROC-01** | **Probe 11.2 Stage B 预算熔断恢复** | 熔断标记 blocked_budget；次轮 0 新条目下自动补跑 Stage B；合法空 impacts 记为 succeeded_empty | **PASSED** |
| **PROC-02** | **Probe 11.2 人审与 RawItem 穿透** | Schema 失败送人审时，`raw_item` 必须已入库，`r.raw_item_id` 非空，原文与源信息完整可读 | **PASSED** |
| **PROC-03** | **ProcessingJob 生命周期流转** | 正常流转下 stage_a_extract -> completed, stage_b_analyze -> completed/succeeded_empty | **PASSED** |
| **PROC-04** | **孤立与悬空数据修复扫描** | Dry-run 正确识别悬空 review 与孤立 event；执行修复后标记 legacy_payload_missing 并补齐作业 | **PASSED** |

### 3.2 现有游标安全回归 `tests/test_collector_cursor_safety.py` (8/8 Passed)
- 两阶段游标提交、中途 STOP 丢弃未提交游标、Schema 失败正常推进游标等 8 个基础可靠性用例全部通过。
