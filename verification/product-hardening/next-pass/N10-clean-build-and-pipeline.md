# N10 验证报告：干净构建、双进程长跑和发布前流水线

> 执行时间: 2026-09-21
> 环境: Windows PowerShell, Clean Python 3.11 (`.\.acceptance\clean-env\Scripts\python.exe`), Node.js v24.16.0
> 关联计划: `docs/Trace_后续修正精确执行计划_2026-09-21.md` 第 13 节 (N10)

---

## 1. 验证目标与交付项

根据规划书 N10 要求：
1. **干净虚拟环境与 Hash 锁定安装**:
   - 依赖解析来自 `requirements-core.lock`（`--require-hashes`），与 Dockerfile 以及 CI 统一，不依赖任何开发态未锁定的包或本地 `.venv`。
2. **测试套件全量覆盖与双语言 CI 强化**:
   - Python 全套测试执行：480 项全部通过（0 失败，1 第三方 Starlette deprecation 警告），完整输出保存在 `verification/product-hardening/next-pass/pytest-full.txt`。
   - Node.js 双测试套件执行：
     - `miniprogram/tests/detail_mapping.test.js`（7 项用例）
     - `miniprogram/tests/session_research.test.js`（会话轮换、并发过期刷新、缓存隔离、公开凭据）
     - 完整输出保存在 `verification/product-hardening/next-pass/node-tests.txt`。
   - 更新 `.github/workflows/ci.yml`，同时执行两个 Node 测试脚本。
3. **双进程同库实证 (`scripts/verify_local_deployment.py`)**:
   - 启动 API (`uvicorn trace.api.app:app`) 与 Background Runner (`python -m trace.main run`) 运行于同一共享数据库文件。
   - 成功探测 `/ready`，验证 31 项 migration 全部生效、46 项证券主数据装载。
   - 验证 guest 会话签发、私有研究问题建立、公开分享 404、研究数据导出完整性、未授权 `/status` 拦截（401 Unauthorized）。
   - 验证 Runner 完成至少 1 轮完整流水线调度。
   - 正常终止两进程树，严格验证端口（动态端口）已完全释放。
   - 产出归档至 `verification/product-hardening/next-pass/local-process-smoke.json`。
4. **探针体系强化与生命周期隔离**:
   - 新增 `/live` 存活探针（200 OK 且极低开销）。
   - 增强 `/ready` 就绪探针：严格比对已应用版本与文件系统迁移脚本集合一致性。
   - 增强 `/status` 业务深度健康检查（受管理员保护）：
     - `processing_oldest_age_seconds`: 处理队列最老未完结任务年龄；
     - `processing_failed_count`: 失败/超限重试任务数；
     - `outbox_ambiguous_count`: 待人工处置的 ambiguous 状态条数；
     - `latest_source_data_at`: 最新抓取数据时间；
     - `latest_backup`: 最新备份快照状态。
   - 资源生命周期：在 `AppContext` 中提供 `close()`，在 FastAPI `lifespan` 退出钩子中主动关闭数据库连接，防止无界泄漏。
5. **容器环境现实状态审查**:
   - 经实测，当前本地执行环境未安装或未运行 Docker daemon（`docker version` 命令无法找到）。
   - 按照真实性原则，容器验收状态记录为 **`PENDING (BLOCKED_BY_EXTERNAL_GATE)`**，严禁伪造“容器已通过”。
   - 准备好完整 `Dockerfile`（多阶段构建、非 root 用户运行、HEALTHCHECK 就绪探针）、`docker-compose.yml`（api 与 runner 两服务配置及卷编排）及后续执行命令。

---

## 2. 执行结果汇总

| 验证项 | 预期要求 | 实测结果 | 证据位置 |
|---|---|---|---|
| Python 测试套件 | 全部通过，0 挂起 | 480 passed, 1 warning (100% 通过) | `verification/product-hardening/next-pass/pytest-full.txt` |
| Node.js 测试套件 | 运行 2 个脚本并全部通过 | 2/2 脚本全部通过 | `verification/product-hardening/next-pass/node-tests.txt` |
| CI 流程更新 | Matrix 覆盖双平台并运行双 Node 脚本 | `.github/workflows/ci.yml` 已配置 | `.github/workflows/ci.yml` |
| 双进程长跑演练 | API + Runner 共享数据库、端口释放 | PASS (profile: production-maintenance) | `verification/product-hardening/next-pass/local-process-smoke.json` |
| 存活/就绪/业务探针 | `/live`, `/ready`, `/status` 三级解耦 | 已集成并通过测试 | `trace/api/routers/status.py` |
| 容器环境执行 | Docker 实际构建与启动 | PENDING (本地无 Docker Runtime) | 本报告第 3 节详细说明 |

---

## 3. 容器化部署待执行指引 (当 Docker Runtime 具备时)

由于宿主机环境缺少 Docker 工具链，待目标服务器或 CI Docker Runner 具备条件后，执行以下命令进行真实容器验收：

```bash
# 1. 验证 Docker 守护进程
docker info

# 2. 构建镜像并验证非 root 权限与依赖锁校验
docker compose build --no-cache

# 3. 后台启动完整服务栈 (api + runner)
docker compose up -d

# 4. 查看服务状态与健康检查结果
docker compose ps

# 5. 检查日志输出与就绪探针
docker compose logs api
docker compose logs runner

# 6. 清理容器
docker compose down -v
```
