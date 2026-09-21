# 任务交付总结：T14（P1 · 业务健康、正式部署与恢复演练）

## 1. 任务背景与核心缺陷
- **关联缺陷**：
  - **F20（备份恢复不可用）**：原项目仅有单向的 `create_backup`，缺少恢复脚本、缺乏数据一致性检验与定期的恢复演练，备份沦为"只存不测"的摆设。
  - **F21（健康检查掩盖真实异常）**：原 `/health` 仅简单判断数据源行状态，无法反映数据源长期无抓取心跳、LLM 预算耗尽、Outbox 消息积压与数据库延迟等真实业务退化状态。
  - **F22（Docker Compose 与部署说明脱节）**：缺乏开箱即用的 `Dockerfile`、`docker-compose.yml` 持久化卷挂载与依赖启动编排。
  - **F26（文档过度承诺生产就绪）**：文档充斥空泛概念，缺少真实可执行的单机部署指南、冷热备 Cron 配置与故障处置 SOP。

---

## 2. 核心改动范围

### 2.1 数据库恢复与自动化演练 (F20)
- **`trace/db/backup.py` 增强**：
  - `restore_backup(backup_path, target_db_path, verify=True)`：通过底层 `sqlite3.backup` API 实现原子热还原，并自动调用校验；
  - `verify_database_integrity(db_path)`：执行 `PRAGMA integrity_check;`、`PRAGMA foreign_key_check;`，并统计核心表（`event`, `event_impact`, `raw_item`, `forecast_snapshot`, `security`, `schema_version` 等）行数与最新时间戳。
- **跨平台运维与演练脚本**：
  - `scripts/restore_drill.py`：自动备份活动库、恢复至隔离演练库、双向比对表行数与一致性并输出审计报告；
  - `scripts/backup.sh` / `scripts/backup.ps1`：支持 Linux/Windows 的热备脚本；
  - `scripts/restore.sh` / `scripts/restore.ps1`：支持恢复与校验的自动化脚本；
  - `verification/product-readiness/T14/restore_drill.md`：记录演练通过的权威凭证。

### 2.2 业务级健康与就绪检查 (F21)
- **`trace/api/schemas.py` 结构扩展**：
  - `HealthResponse`：增加 `db_status`、`db_latency_ms`、`sources_status`、`sources_stale_count`、`llm_budget_used_pct`、`outbox_pending_count`、`diagnostics` 字段；
  - `ReadyResponse`：定义 `/ready` 就绪探针响应结构。
- **`trace/api/routers/status.py` 探针强化**：
  - **数据库健康**：实时探测 `PRAGMA quick_check;` 与查询往返耗时，异常时标记 `UNHEALTHY`；
  - **抓取心跳新鲜度**：若所有启用数据源超过 1 小时无成功心跳，状态自动降级为 `DEGRADED` 并输出排查告警；
  - **LLM 预算熔断**：当日预算达到 100% 时标记 `DEGRADED`，提示已自动降级为规则抽取；
  - **Outbox 队列积压**：待发送队列积压 > 50 时标记 `DEGRADED`；
  - **`/ready` 探针**：校验数据库可达性、20 个 migration 全量应用、证券基础主数据加载；不就绪时返回 HTTP 503。
- **`trace/api/app.py`**：将探针同时挂载于根路径（`/health`, `/ready`）与版本路径（`/api/v1/health`, `/api/v1/ready`），适配 Docker/K8s 标准容器探针。

### 2.3 正式生产部署与运维材料 (F22 / F26)
- **`Dockerfile`**：
  - 多阶段构建，基于 `python:3.11-slim-bookworm`；
  - 生产安全非 root 用户 `trace` (UID 1000)；
  - 挂载持久化目录 `/app/data` 与 `/app/logs`；
  - 内置容器级 `HEALTHCHECK`。
- **`docker-compose.yml`**：
  - 双服务编排：`api`（FastAPI 服务，8000 端口）与 `runner`（长驻调度引擎）；
  - 依赖健康状态启动：`runner` 依赖 `api` 的 `service_healthy`；
  - 数据卷命名持久化：`trace_data` 与 `trace_logs`；
  - 故障自愈策略：`restart: unless-stopped`。
- **`.env.example`**：
  - 分区规范配置：系统模式（`real` vs `offline`）、安全密钥、LLM 预算熔断、Alpaca 行情凭据、Telegram 机器人配置、SEC EDGAR 机构合规报备。
- **`docs/deployment_guide.md`**：
  - 编制严谨的单机 Linux / 云主机部署指南、定时 Cron 备份 SOP、灾难恢复演练流程与故障排查诊断 Playbook。

---

## 3. 测试与验收证据

### 3.1 自动化测试矩阵 (`tests/test_health_and_deployment.py`)
1. `test_health_check_healthy_state`：所有数据源心跳正常时，返回 `HEALTHY` 且诊断项为空；
2. `test_health_check_stale_sources_degraded`：模拟超时停抓（心跳 > 1 小时），接口正确降级为 `DEGRADED` 并输出排查提示；
3. `test_health_check_llm_budget_exhausted_degraded`：模拟 LLM 预算达到 100%，正确标记 `DEGRADED`；
4. `test_health_check_outbox_backlog_degraded`：模拟 Outbox 积压 > 50，正确标记 `DEGRADED`；
5. `test_readiness_probe`：正常状态返回 200 READY；清空主数据时阻断返回 503 NOT_READY；
6. `test_backup_restore_and_automated_drill`：自动化执行恢复演练，源库与恢复库全表行数一致，完整性校验为 `ok`；
7. `test_docker_compose_and_env_example`：验证 compose 卷挂载与健康检查合法，.env.example 关键字段完整。

### 3.2 测试执行结果
```bash
$ .\.venv\Scripts\python.exe -m pytest tests/test_health_and_deployment.py -v
============================== 7 passed in 2.73s ==============================

$ .\.venv\Scripts\python.exe -m pytest tests/test_api.py -v
============================= 13 passed in 5.30s ==============================
```

## 4. 交付物清单
1. `trace/db/backup.py` (`restore_backup`, `verify_database_integrity`)
2. `trace/api/schemas.py` (`HealthResponse`, `ReadyResponse`)
3. `trace/api/routers/status.py` (增强 `/health` 与 `/ready`)
4. `trace/api/app.py` (根路径探针挂载)
5. `scripts/restore_drill.py` (灾难恢复自动化演练工具)
6. `scripts/backup.sh`, `scripts/backup.ps1`
7. `scripts/restore.sh`, `scripts/restore.ps1`
8. `Dockerfile`
9. `docker-compose.yml`
10. `.env.example`
11. `docs/deployment_guide.md`
12. `tests/test_health_and_deployment.py`
13. `verification/product-readiness/T14/restore_drill.md` (演练执行凭单)
14. `verification/product-readiness/T14/summary.md` (本总结报告)
