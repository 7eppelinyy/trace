# N02 会话续期、凭据轮换与账户/缓存隔离实施报告

- **日期**：2026-09-21
- **优先级**：P0
- **涉及迁移**：`0028_session_refresh_lifecycle.sql`
- **状态**：PASS

## 1. 背景与缺陷治理 (RV01 返工项)
此前系统使用固定 30 天 access token，过期后前端抛错强制用户重置席位，导致服务端旧数据（自选、研究草案等）丢失访问关联。
同时，旧版前端存在全局 `cached_watchlist` 等未按后端与账户隔离的本地缓存键，且在开发模式下曾出现生产模式 dev grant 逻辑冲突与非法 `TRACE_MODE` 检验漏放等问题。

## 2. 核心架构与协议实现

### 2.1 数据库迁移 (`0028_session_refresh_lifecycle.sql`)
- 新建 `user_refresh_token` 表：
  - `token_hash`: 刷新凭据的 SHA-256 哈希作为主键（服务端严禁存储明文凭据）；
  - `user_id`: 关联用户主体；
  - `family_id`: 令牌族标识（用于令牌轮换与防重放攻击追踪）；
  - `session_token`: 关联会话 token；
  - `expires_at`: 90 天到期时效；
  - `used_at`: 单次消费记录；
  - `revoked_at`: 撤销记录。
- `user_session` 追加 `family_id` 字段，实现 access 与 refresh 生命周期与撤销联动。

### 2.2 服务端认证接口 (`trace/api/routers/auth.py`)
- `POST /auth/session`：
  - guest: 服务端签发随机安全 `user_id`（`usr_...`），返回 `session_token` + `refresh_token` + `expires_at`；
  - dev: 仅在非生产且明确设置 `TRACE_ALLOW_DEV_AUTH=1` 时允许，生产模式严格返回 403；
  - wechat: 未接入真实平台 code 校验时明确返回 501；
  - 采用独立速率限制桶（`session-ip` 20/h, `session-global` 500/h）。
- `POST /auth/session/refresh`：
  - 接收 `{ refresh_token: str }`；
  - 独立限流桶（`session-refresh-ip` 60/h, `session-refresh-global` 1000/h），活跃用户续期不被访客注册挤占；
  - 短事务原子轮换：验证未过期、未撤销、未使用；轮换签发新 session 及新 refresh token，**严格保持 user_id 不变**；
  - **防重放攻击 (Token Replay Defense)**：若检测到 `used_at` 已经有值的旧 refresh token 被重复提交，自动触发熔断，撤销同 `family_id` 下的所有 refresh token 与活跃 session，并返回 401。
- `DELETE /auth/session`：
  - 撤销当前 session 并联动撤销对应 refresh family。

### 2.3 客户端单例协调与隔离 (`miniprogram/utils/api.js`)
- `refreshSessionToken()` 单例协调 Promise（`refreshPromise`）：
  - 并发请求检测到 session 过期或遇到 401 时，统一排队等待同一个刷新请求，保证单个 refresh token 只被原子消费一次；
  - 轮换成功后原子更新本地会话存储；
- 缓存命名空间隔离：
  - 所有业务本地存储键改造为 `${prefix}:${baseUrl}:${userId}`（如 `cached_watchlist:https://...:usr_...`）；
  - 重置时清理当前后端和当前用户的私有缓存，防止多后端/多会话数据污染。
- 设置页面 (`profile.wxml` / `profile.js`)：
  - 清理硬编码演示账户标识与假席位编号；
  - 正确显示设备访客账户标识，未配置微信推送时明确标注文案。

## 3. 验证证据
1. `tests/test_auth_lifecycle.py`：
   - 凭据签发与 refresh_token 返回验证 (PASS)
   - 续期前后 user_id 严格一致、自选与私有研究数据完整继承 (PASS)
   - 防重放攻击熔断：旧 refresh 重放触发整个 family 撤销，新 session 与新 refresh 均失效 (PASS)
   - 主动撤销 DELETE /auth/session 验证 (PASS)
   - 生产模式 dev 拒绝 (403) 与 guest 放行 (PASS)
   - 非法 TRACE_MODE 启动阻断验证 (PASS)
2. `miniprogram/tests/session_research.test.js`：
   - 并发请求自动刷新协调 Promise 验证 (PASS)
   - 账户缓存隔离与重置清除验证 (PASS)
