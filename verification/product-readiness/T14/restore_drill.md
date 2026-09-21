# 自动化灾难恢复演练记录 (Restore Drill Report)

- **演练时间**：2026-09-19T09:10:07Z
- **演练执行脚本**：`scripts/restore_drill.py`
- **执行命令**：`python scripts/restore_drill.py --source data/trace.db --backup-dir data/backups --target data/restore_drill_tmp.db`
- **演练结论**：`PASS`（100% 数据一致性与完整性无损恢复）

---

## 1. 演练环境与路径
| 参数项 | 路径 / 取值 | 说明 |
| :--- | :--- | :--- |
| **源数据库 (Source DB)** | `data/trace.db` | 当前活跃生产/测试 SQLite 数据库 |
| **备份目录 (Backup Dir)** | `data/backups/` | 存储一致性快照目录 |
| **生成备份快照** | `data/backups/trace-20260919-091007.db` | 快照大小: 487,424 字节 |
| **临时恢复演练库** | `data/restore_drill_tmp.db` | 隔离目标库（演练完毕后安全清理） |
| **演练耗时** | 0.248 秒 | 在线热备 API (`sqlite3.backup`) |

---

## 2. 完整性检查比对 (`PRAGMA`)

| 检查项 | 源数据库 (Source) | 恢复数据库 (Restored) | 比对结果 |
| :--- | :--- | :--- | :--- |
| `PRAGMA integrity_check;` | `ok` | `ok` | 一致 (PASS) |
| `PRAGMA foreign_key_check;` | `ok` (0 violations) | `ok` (0 violations) | 一致 (PASS) |

---

## 3. 关键业务表行数一致性审计

| 数据表名 | 源库记录数 (Source Count) | 恢复库记录数 (Restored Count) | 差异量 |
| :--- | :--- | :--- | :--- |
| `schema_version` | 20 | 20 | 0 |
| `security` | 44 | 44 | 0 |
| `source` | 13 | 13 | 0 |
| `event` | 12 | 12 | 0 |
| `event_impact` | 24 | 24 | 0 |
| `raw_item` | 48 | 48 | 0 |
| `forecast_snapshot` | 16 | 16 | 0 |
| `forecast_check` | 16 | 16 | 0 |

---

## 4. 演练过程摘要
1. **源库在线锁保护验证**：
   - 采用 SQLite 官方底层一致性快照 API（`src.backup(dst)`），在 WAL 模式活动库写入期间安全导出，绝不发生锁表阻塞或半写入脏读。
2. **目标库重构与回放**：
   - 备份文件完整写入临时库 `data/restore_drill_tmp.db`；
   - 触发 `verify_database_integrity` 自动校验，确认无损坏扇区或孤立索引。
3. **环境收敛与清理**：
   - 演练结束后，临时库 `restore_drill_tmp.db` 被原子解绑并删除，磁盘空间零残留。
