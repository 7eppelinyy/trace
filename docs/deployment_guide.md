# Trace 生产部署与单机运维 SOP 指南 (T14 / F22 / F26)

本指南针对 Trace 系统的单机 Linux（Ubuntu 22.04 / 24.04 LTS 或 Debian 12）以及容器化云主机部署提供标准规范与运维 SOP，杜绝脱离实际的口头承诺。

---

## 1. 架构与组件设计

Trace 采用轻量化单机双进程容器架构：
- **`trace-api`**：FastAPI 服务，监听 `8000` 端口，提供 Webhook、微信小程序 REST API、`/health` 与 `/ready` 探针。
- **`trace-runner`**：后台长驻引擎 (`python -m trace.main --loop`)，按调度周期抓取数据源、执行 Stage A/B 智能流水线、更新市场确认、维护回测账本并执行 Telegram 告警。
- **持久化存储卷**：
  - `trace_data`：挂载至 `/app/data`，存放 SQLite 数据库文件 (`trace.db`) 及其 WAL 文件与快照备份 (`backups/`)。
  - `trace_logs`：挂载至 `/app/logs`，记录系统各组件运行日志。

---

## 2. 部署前准备

### 2.1 基础环境要求
- **操作系统**：Ubuntu 22.04+ / Debian 12+ / Rocky Linux 9+
- **硬件规格**：最低 1 核 CPU / 1GB RAM / 20GB SSD（推荐 2 核 / 2GB RAM）
- **软件依赖**：
  - Docker Engine >= 24.0
  - Docker Compose >= 2.20
  - `curl`, `tar`, `sqlite3`

### 2.2 配置环境变量
克隆仓库并基于模板生成生产环境配置文件：
```bash
cd /opt/trace
cp .env.example .env
chmod 600 .env
vim .env
```
重点配置项：
1. `TRACE_MODE=real`（生产真实模式）；
2. `APP_SECRET`：设置为随机高强度字符串；
3. `GEMINI_API_KEY` 或 `OPENAI_API_KEY`；
4. `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_ALLOWED_CHAT_IDS`；
5. `SEC_COMPANY_NAME` 与 `SEC_CONTACT_EMAIL`（SEC EDGAR 抓取合规要求）。

---

## 3. 生产部署步骤

### 3.1 启动服务
```bash
# 构建镜像并启动双容器
docker compose up -d --build

# 检查容器运行状态
docker compose ps
```

### 3.2 验证健康与就绪探针
```bash
# 检查就绪探针（必须返回 HTTP 200 与 READY 状态）
curl -i http://localhost:8000/ready

# 检查详细业务健康度
curl -s http://localhost:8000/health | jq .
```
健康响应样例：
```json
{
  "status": "HEALTHY",
  "db_status": "OK",
  "db_latency_ms": 0.45,
  "sources_status": "OK",
  "sources_stale_count": 0,
  "llm_budget_used_pct": 12.5,
  "outbox_pending_count": 0,
  "diagnostics": [],
  "sources": [...]
}
```

---

## 4. 数据库备份与灾难恢复 SOP (F20)

Trace 使用 SQLite 在线热备 API (`sqlite3.backup`)，备份期间不锁写、不中断业务。

### 4.1 定时自动备份 (Cron)
在宿主机配置每日凌晨 02:00 自动备份并轮换保留最近 7 份：
```bash
# 编辑宿主机 crontab
crontab -e

# 添加如下定时任务：
0 2 * * * docker compose -f /opt/trace/docker-compose.yml exec -T runner /app/scripts/backup.sh >> /var/log/trace_backup.log 2>&1
```

### 4.2 灾难恢复演练 SOP
当主数据库异常或需要执行数据回滚时，按以下标准流程操作：

1. **停止数据写入进程**：
   ```bash
   docker compose stop runner
   ```

2. **定位最近可用的备份文件**：
   ```bash
   docker compose exec api ls -lt /app/data/backups/
   ```

3. **执行恢复脚本**：
   ```bash
   # 自动恢复并执行 PRAGMA integrity_check 与行数比对校验
   docker compose exec api /app/scripts/restore.sh /app/data/backups/trace-20260919-020000.db /app/data/trace.db
   ```

4. **恢复演练自动化验证**：
   在不中断生产的情况下，随时可执行独立临时库的还原演练：
   ```bash
   docker compose exec api python /app/scripts/restore_drill.py
   ```

5. **重启业务进程**：
   ```bash
   docker compose start runner
   curl -s http://localhost:8000/ready
   ```

---

## 5. 故障排查与诊断 Playbook (F21)

当 `/health` 状态呈现 `DEGRADED` 或 `UNHEALTHY` 时，参考 `diagnostics` 字段进行针对性处置：

| 异常表现 | 诊断信息提示 | 排查与处置步骤 |
| :--- | :--- | :--- |
| `sources_status: STALE` | `All enabled sources have no successful fetch within the last 1 hour` | 1. 检查宿主机出网连接与 DNS 解析；<br>2. 检查抓取源是否触发反爬封锁；<br>3. 检查 `trace-runner` 容器日志：`docker compose logs -n 100 runner`。 |
| `llm_budget_used_pct: 100%` | `LLM call budget exhausted for today` | 当日 LLM 成本保护熔断。系统已自动降级为规则抽取与评分；若需临时提高上限，可在 `.env` 中调大 `LLM_DAILY_CALL_BUDGET` 并重启 runner。 |
| `outbox_pending_count > 50` | `Alert outbox backlog high` | 消息投递积压。检查 Telegram Bot Token 是否有效、Telegram 网络代理连通性或接收者 Chat ID 配置。 |
| `db_status: ERROR / CORRUPTED` | `Database query failed` | SQLite 数据库文件受损或权限异常。执行恢复 SOP 还原最新一份备份。 |
