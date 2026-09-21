# N03 验收记录：作业、投递和人工复核的运维闭环

> 验收日期：2026-09-21  
> 责任阶段：N03（作业、投递和人工复核运维闭环）  
> 状态：**PASS**

---

## 1. 目标与实现概况

本阶段按照《Trace 后续修正精确执行计划》第 6 节要求，对主数据流水线与交付链路的四个核心事务边界进行了严格核对与测试覆盖，补齐了多进程作业租约竞争、长耗时任务租约续期、单实例运行控制、Ambiguous 状态受控处置与审计痕迹、以及人工检查安全重试机制。

---

## 2. 关键设计与变更清单

### 2.1 四个事务边界的严格核对
1. **边界 1 (Raw + Extraction Intent)**:
   - 位置: `trace/pipeline.py` (lines 246–258)
   - 机制: 在 `with ctx.db.transaction(mode='IMMEDIATE')` 事务内同时持久化 `RawItem` 与入队 `stage_a_extract` 处理作业。游标仅在条目与意图共同提交后提交 (`commit_cursors`)；中断则丢弃未提交游标 (`discard_cursors`)。
2. **边界 2 (Event Version + Analysis Intent)**:
   - 位置: `trace/event_engine/engine.py` (lines 149–165, 198–210)
   - 机制: 在 `with self.db.transaction()` 短事务内同时完成事件创建/修订、向量落库、证据关联与 `stage_b_analyze` 作业调度，并在事务提交完成时调用 `on_commit` 标记 `stage_a_extract` 为 `completed`。
3. **边界 3 (Analysis Result + Notification Intent)**:
   - 位置: `trace/pipeline.py` (lines 327–356)
   - 机制: 模型与行情计算在事务外部完成；在 `with ctx.db.transaction(mode='IMMEDIATE')` 短事务内，校验租约归属 (`job_repo.owns(job)`) 与事件版本一致性 (`latest.version == event.version`)，落库影响分析结果 (`persist_results`)、记录 `analysis_run`、评估告警并将提醒入队至 `alert_outbox`，最后原子标记作业为 `completed`。
4. **边界 4 (Delivery Terminal Status + Receipt)**:
   - 位置: `trace/alerts/delivery_worker.py` (lines 70–80)
   - 机制: 外部网络请求在事务外执行；在 `with db.transaction(mode='IMMEDIATE')` 短事务内，校验租约并原子更新 Outbox 终态 (`sent`/`failed`) 与持久化交付回执 (`alert_delivery`)。

### 2.2 真实双进程作业租约竞争与超时保护
- **实现**: `tests/test_durable_operational_loop.py::test_multiprocess_job_lease_competition`。
- **机制**: 派生两个真实 OS 子进程，使用同一 SQLite 文件数据库。Worker 1 认领作业并持有 2 秒租约，随后休眠 3.5 秒。Worker 2 在第 2.5 秒认领已过期的作业并成功完成。Worker 1 唤醒后尝试调用 `finish`，被底层租约归属校验严格拦截并抛出 `RuntimeError('Processing lease lost')`，杜绝旧进程写回覆盖新结果。

### 2.3 长耗时模型任务租约续期机制
- **实现**: `ProcessingJobRepo.renew_lease(job, additional_seconds=600)` (`trace/db/jobs.py`)。
- **机制**: 针对模型调用耗时较长的场景，提供严格基于 `lease_owner` 和未过期 `lease_until` 的租约续期能力。冒名者或租约已失效的任务续租请求会被严格抛出异常拒绝。

### 2.4 单实例流水线运行控制
- **实现**: `SingleInstanceLock` (`trace/common/process_lock.py`) 与 `Pipeline.run_forever`。
- **机制**: 基于原生操作系统级文件锁（Windows 下使用 `msvcrt.locking`，Unix 下使用 `fcntl.flock`）。在流水线轮询长驻进程启动时对 `<data_dir>/runner.lock` 加独占锁。若已有活动 runner，立即抛出明确异常阻止双实例并发导致事件重复聚类或游标踩踏；当进程退出或异常崩溃时，操作系统内核自动无残留释放文件锁。

### 2.5 Ambiguous Outbox 受控处理与审计日志
- **数据迁移**: `0029_outbox_audit_and_management.sql` 创建 `outbox_audit_log` 审计表。
- **核心能力**:
  - `AlertOutboxRepo.list_ambiguous(limit=100)`: 查看待处理的不确定状态任务。
  - `AlertOutboxRepo.resolve_ambiguous(outbox_id, action, operator, note)`:
    - `confirm_delivered`: 确认为已送达，更新状态为 `sent` 并补全 `alert_delivery` 回执。
    - `requeue`: 明确允许潜在重复，重置状态为 `pending` 并归零重试计数，重新由 worker 认领发送。
    - `discard`: 放弃投递，更新状态为 `suppressed`。
    - 强制记录操作人 (`operator`)、前后状态转换与备注至 `outbox_audit_log`。
  - 严禁后台无声自动重发；非 ambiguous 状态或缺失操作人的请求严格报错拒绝。
- **入口支持**:
  - CLI: `python -m trace.main outbox --list-ambiguous`、`python -m trace.main outbox --resolve <id> --action <confirm_delivered|requeue|discard> --operator <name> --note <reason>`。
  - REST API: `GET /api/v1/admin/outbox/ambiguous`、`POST /api/v1/admin/outbox/ambiguous/{id}/resolve`、`GET /api/v1/admin/outbox/audit`（受 `require_admin` 保护）。

### 2.6 人工检查安全重试防线
- **实现**: `HumanReviewRepo.retry(review_id)` (`trace/db/health.py`)。
- **防线**:
  - 缺失原始 `raw_item` 或 `event` 载荷时，严格报错拒绝重试。
  - 正在运行 (`processing`) 或已完成 (`completed`) 的作业严格禁止覆盖。
  - 重试将作业状态重置为 `pending`，保留 `human_review` 中的原始失败原因与日志，仅将审查状态流转为 `retried`。

### 2.7 投递旁路审查与受控例外界定
- **审查结论**:
  1. **Bot 命令回复 (`/help`, `/watch`, `/ask`)**: 属于 Telegram 会话内的同步 RPC 应答，由 Telegram 轮询框架在当前会话中即时回包，非后台异步事件通知，作为显式受控例外记录。
  2. **验收回放命令 (`replay-event`)**: 属于开发者/QA 在终端调用的离线真实回放验收工具，带有 `--acceptance-test` 显式参数，同步捕获回执并输出至 stdout 与证据文件，作为显式受控例外记录。
  3. **每日摘要 (`digest`)**: 长驻循环维护时生成的全量摘要，通过 `alert_sender` 投递并强制检查 `TraceMode.is_production()` 凭据；同时支持通过 `AlertOutbox` 进行可靠排队并已在 `delivery_policy` 中放行摘要类型。

---

## 3. 验证结果

### 3.1 N03 专项测试
命令:
```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -v tests/test_durable_operational_loop.py
```
结果:
```text
tests/test_durable_operational_loop.py::test_processing_job_lease_renewal PASSED [ 14%]
tests/test_durable_operational_loop.py::test_single_instance_runner_lock PASSED [ 28%]
tests/test_durable_operational_loop.py::test_multiprocess_job_lease_competition PASSED [ 42%]
tests/test_durable_operational_loop.py::test_ambiguous_outbox_resolution_and_audit PASSED [ 57%]
tests/test_durable_operational_loop.py::test_human_review_retry_safeguards PASSED [ 71%]
tests/test_durable_operational_loop.py::test_daily_digest_execution_and_outbox_policy PASSED [ 85%]
tests/test_durable_operational_loop.py::test_delivery_worker_continues_under_model_stop PASSED [100%]
7 passed in 5.90s
```

### 3.2 N03 全量边界回归套件
命令:
```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q tests/test_durable_boundaries.py tests/test_processing_recovery.py tests/test_collector_cursor_safety.py tests/test_outbox.py tests/test_pipeline.py tests/test_human_review.py tests/test_durable_operational_loop.py
```
结果:
```text
44 passed in 13.12s
```

### 3.3 全库单元测试套件回归
命令:
```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
结果:
```text
421 passed, 1 warning in 76.43s
```

### 3.4 小程序客户端与契约测试
命令:
```powershell
node miniprogram/tests/detail_mapping.test.js
node miniprogram/tests/session_research.test.js
```
结果:
```text
=== All Detail Mapping & Authenticity Tests Passed! ===
Session, token refresh, and research client contracts passed.
```
