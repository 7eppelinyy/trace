# N09 验证报告：有历史数据的迁移、预测口径和恢复

> 执行时间: 2026-09-21
> 环境: Windows PowerShell, Clean Python 3.11 (`.\.acceptance\clean-env\Scripts\python.exe`)
> 关联计划: `docs/Trace_后续修正精确执行计划_2026-09-21.md` 第 12 节 (N09)

---

## 1. 验证目标与交付项

根据规划书 N09 要求：
1. **有真实相互关联数据的 0022 库升级**:
   - 包含 `user`, `user_session`, `watchlist`, `source`, `raw_item`, `event`, `event_source`, `event_impact`, `research_question`, `processing_job`, `alert_outbox`, `forecast_snapshot`, `forecast_check`。
   - 顺利应用 0023 至 0031 迁移。
   - 重点验证 0025 `raw_item` 与 `processing_job` 表重建（Rebuild）无行数丢失、索引完整、外键无断裂。
2. **DDL 原子性与中断回滚**:
   - 验证异常中断时版本回执与 DDL 语句完整回滚，不留下半套 schema。
   - 采用 SQLite 官方 12 步安全建表策略（事务前关闭外键并在提交前执行 `PRAGMA foreign_key_check`），确保跨版本并发无外键阻塞与悬挂约束。
3. **预测口径隔离与基准新鲜度规则**:
   - 0026 `legacy_unverified` 标记历史不可信预测，完全从新口径实测率（`measurement_rate`）和命中率（`hit_rate`）中剔除，但在数据库中保持完整可审计。
   - 基准大盘指数（SPX / STAR50）的 anchor 与 exit 行情严格遵守与个股相同的可靠性（`quote_quality == 'real'`）与时间新鲜度检验，拒绝 mock 或陈旧数据虚构超额收益。
4. **备份恢复与灾难演练 (CLI & 脚本)**:
   - `scripts/db_admin.py` 与 `scripts/restore_drill.py` 目标必须为不存在的新路径，严格拒绝覆盖运行中数据库。
   - 路径含空格与中文（`测试 空间 2026/源 业务 库.db`）作为原生参数安全处理。
   - 恢复过程提供 `--quarantine-outbox` 机制，将旧库中原处于 `sending` 状态的项目安全转移为 `ambiguous`（标注 `restored_from_backup_quarantine`），彻底杜绝旧库回滚后的突发重复告警。
   - 破坏性负例验证：外键破坏、核心表缺失、损坏/截断文件、不存在路径均明确报错（FAIL / Exception），严禁虚构种子库假冒 PASS。
   - 恢复演练生成全量 38 张业务表 SHA256 内容哈希一致性比对报告，记录真实 RTO（0.111s）与 RPO 声明。

---

## 2. 执行结果与证据

### 2.1 自动化测试矩阵 (`tests/test_n09_migration_data_and_restore.py`)

```text
tests/test_n09_migration_data_and_restore.py::test_migration_0022_to_latest_preserves_interconnected_data PASSED
tests/test_n09_migration_data_and_restore.py::test_migration_atomic_rollback_on_failure PASSED
tests/test_n09_migration_data_and_restore.py::test_legacy_forecast_isolation_and_strict_benchmark_rules PASSED
tests/test_n09_migration_data_and_restore.py::test_db_admin_backup_restore_cli_with_special_paths PASSED
tests/test_n09_migration_data_and_restore.py::test_negative_drill_cases_fail_explicitly PASSED
tests/test_n09_migration_data_and_restore.py::test_outbox_quarantine_on_restore PASSED
tests/test_n09_migration_data_and_restore.py::test_restore_drill_cli_produces_full_report PASSED

============================== 7 passed in 2.94s ==============================
```

### 2.2 恢复演练实际运行报告 (`restore-proof.json`)

```json
{
  "drill_status": "PASS",
  "timestamp": "2026-09-21T04:55:45.784494+00:00",
  "duration_seconds": 0.111,
  "rto_seconds": 0.111,
  "rpo_basis": "snapshot_at_backup_creation",
  "latest_event_time": "2026-09-21T04:55:28.340403+00:00",
  "drill_mode": "local_isolated_recovery_verification",
  "source_db": "data\\tmp_n09_source.db",
  "backup_file": "data\\tmp_n09_backups\\trace-20260921-045545-801605.db",
  "drill_target": "data\\tmp_n09_drill_target.db",
  "mismatches": []
}
```

### 2.3 迁移与数据保全证明 (`migration-proof.json`)
- 0022 库包含 12 类关联实体，迁移至 0031 后：
  - `integrity_check`: ok
  - `foreign_key_check`: ok
  - `raw_item` 关联数据完全保全，重建后外键索引正常。
  - `processing_job` 重建后任务恢复为可重试状态。
  - `alert_outbox` 中原 `sending` 项自动降级为 `ambiguous` 待审状态。

---

## 3. 验收结论

N09 阶段所涉及的代码加固、数据迁移保证、预测真实性规则、灾难恢复与演练机制全部通过自动化测试与物理命令检验，符合标准发布要求。
