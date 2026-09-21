# T02 · 服务端身份、对象授权与管理员边界 验证报告

## 1. 任务概述
- **任务编号**: T02 (P0)
- **关联问题**: F02（伪造身份与无限自选）、F14（API 鉴权缺失）、F26（安全边界审查）
- **核心目标**: 建立可信服务端会话凭据机制，消除客户端任意 `X-User-Id` 信任，实施严格对象级授权（A 凭据冒用 B 身份直接 403），敏感运维路径脱敏，小程序端全自动会话协商与容灾。

---

## 2. 核心架构与代码变更

### 2.1 数据库迁移与模型
- **SQL 迁移**: `trace/db/migrations/0014_user_session.sql`
  - 新增 `user_session` 表：包含 `session_token` (主键，32 字节高熵随机串)、`user_id`、`created_at`、`expires_at`、`is_revoked`、`device_info`。
- **领域模型与仓库**: `trace/domain/models.py` (`UserSession`) 与 `trace/db/repositories.py` (`UserSessionRepo`)。
  - 支持 `create_session`、`get_session`、`revoke_session`。

### 2.2 鉴权依赖与对象级授权 (`trace/api/deps.py`)
- `get_current_user_id`:
  - 严格校验 `Authorization: Bearer <session_token>`。
  - 检查会话是否存在、是否被撤销 (`is_revoked`)、是否已过期 (`expires_at < now`)。
  - **OWASP 对象授权反越权防护 (Probe 11.4)**：比对请求头 `X-User-Id` 或 URL Query `user_id`。若客户端声称的 ID 与会话真实主体不匹配，严格抛出 HTTP 403 Forbidden。
  - 未携带凭据严格返回 HTTP 401 Unauthorized。

### 2.3 认证路由与生产安全守卫 (`trace/api/routers/auth.py`)
- `POST /api/v1/auth/session`:
  - `grant_type="dev"`: 仅在非生产环境开放，用于自动化测试与研发自测；生产模式 (`TraceMode.is_production()`) 直接拦截并返回 403。
  - `grant_type="guest"`: 服务端分配受控随机 `usr_<hex>` 主体，杜绝客户端撞库或伪造。
  - 短事务内幂等注册用户并初始化核心标的自选池，避免并发争抢。
- `DELETE /api/v1/auth/session`: 撤销当前会话。
- `GET /api/v1/auth/me`: 获取当前主体状态。

### 2.4 敏感信息脱敏 (`trace/api/routers/status.py`)
- `/api/v1/status` 返回的 `db_path` 剥离系统绝对路径与盘符，仅暴露文件名 (`Path(...).name`)，防止服务器路径泄露。

### 2.5 小程序客户端自动接入 (`miniprogram/utils/api.js`)
- 实现 `getSessionToken()` 异步会话池：自动利用本地设备 ID 协商 `trace_session_token` 并持久化。
- `request()` 统一在请求头挂载 `Authorization: Bearer <token>`。
- 捕获服务端 401 凭据失效，自动清空本地失效 Token 并触发单次强制重协商与静默重试。

---

## 3. 测试验证矩阵

### 3.1 专用测试集 `tests/test_authz.py` (7/7 Passed)
| 用例编号 | 验证场景 | 预期行为 | 测试结果 |
| :--- | :--- | :--- | :--- |
| **AUTH-01** | 未登录访问受保护接口 (`/watchlist`, `/ask`, `/digest`) | 401 Unauthorized + WWW-Authenticate | **PASSED** |
| **AUTH-02** | 公开接口访问 (`/`, `/health`, `/events`, `/market/indices`) | 200 OK | **PASSED** |
| **AUTH-03** | 敏感运维路径脱敏 (`/status`) | `db_path` 仅含文件名，不含 `\`、`/`、`:` | **PASSED** |
| **AUTH-04** | 会话生命周期与主动撤销 (`DELETE /auth/session`) | 撤销后再次访问返回 401 | **PASSED** |
| **AUTH-05** | 过期会话访问拦截 (`expires_at < now`) | 401 Unauthorized (Session token has expired) | **PASSED** |
| **AUTH-06** | **Probe 11.4 对象越权篡改防护** (A Token + B Header/Query) | 403 Forbidden 严格拦截冒用 | **PASSED** |
| **AUTH-07** | 生产模式守卫 (`TRACE_MODE=production`) | `grant_type="dev"` 返回 403，仅允许 guest | **PASSED** |

### 3.2 现有 API 兼容性回归 `tests/test_api.py` (13/13 Passed)
- 所有原有自选、问答、投递、大盘指标测试在引入合法 Bearer Token 机制后全部一次性通过。
