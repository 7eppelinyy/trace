# T15 交付总结：证券主数据、覆盖目录与来源治理 (F23/F29/F30)

> **任务编号**：T15  
> **优先级**：P1 (Gate G2 Production Readiness)  
> **关联缺陷**：F23 (证券候选与交易所保真)、F29 (来源许可范围门禁)、F30 (运营 Override 隔离与用户私有别名)  
> **交付日期**：2026-09-19  

---

## 1. 缺陷治理与核心整治成果

### 1.1 证券状态与候选分级 (F23)
- **问题现状**：动态搜索与添加只要正则合法即生成共享上市公司实体；腾讯 Smartbox 美股检索对点号股类执行 `sym.split(".")[0]` 粗暴截断，且盲目将所有美股硬编码写入 `exchange: NASDAQ`；缺少状态生命周期管理。
- **整治方案**：
  - 在 `security` 表与 `Security` 模型中增加 `status` 字段（`SecurityStatus.VERIFIED`、`UNVERIFIED`、`DELISTED`）。
  - 动态候选标的与 Smartbox 结果恒常标记为 `status: unverified`，并在 API 中对应 `coverage_tier: unverified_candidate`，严禁未经官方核验直接生成正式已核验标的。
  - 修复 Smartbox 股类截断，支持 `BRK.B` / `BRK-B` 规范化保真；美股交易所除已知标的（如 `TSM -> NYSE`）外未知保持为空，杜绝假装掌握交易所信息。
  - `SecurityRepo.upsert` 引入保护逻辑：防止 unverified 候选覆盖退市状态（`delisted`）或将已核验主数据（`verified`）冲正为未核验。

### 1.2 来源许可范围门禁与合规元数据 (F29)
- **问题现状**：技术上可访问的数据源缺乏商用获取、存储、展示与转发权限的统一控制，缺少核验责任人。
- **整治方案**：
  - 在 `source` 表增加 4 维布尔门禁：`can_fetch`（获取）、`can_store`（存储）、`can_display`（展示）、`can_forward`（推送/分发）。
  - 增加合规核验字段：`verified_at`、`verified_by`。
  - 对 29 个已登记数据源进行全面核定：
    - 官方监管与政策源：拥有全部四项许可；
    - 金十数据（MCP 授权）：允许获取、存储与内部 AI 推理展示，但严格禁止商业二次转售/转发（`can_forward = false`）；
    - 未获正式商用授权的商业媒体（路透、彭博、道琼斯、FT 等）：四项许可全关（`false`），仅作候选登记，生产严禁静默伪造抓取。

### 1.3 运营状态隔离与持久化保护 (F30)
- **问题现状**：`load_all_seeds` 在每次应用启动时无条件 `upsert`，导致运营人员在运行期手工停用的数据源在服务重启或容器重建后被 seed 配置无情冲正。
- **整治方案**：
  - `source` 表增加 `operational_override`、`override_reason`、`override_updated_at`、`override_updated_by`。
  - `SourceRepo.upsert` 在冲突更新时**严格排除**运营 Override 字段，重启服务不冲正手工停用。
  - 系统抓取与健康探针基于有效启用状态判定：`COALESCE(operational_override, enabled) == 1`。

### 1.4 用户专属自选别名隔离 (F30)
- **问题现状**：原自选添加逻辑在传入公司名称时直接 `upsert` 共享 `Security` 表，普通用户可任意篡改全局上市公司名称。
- **整治方案**：
  - `watchlist` 表增加 `user_alias TEXT NOT NULL DEFAULT ''`，用户个性化别名严格保存在自选记录中。
  - 新增 `PUT /api/v1/watchlist/{ticker}/alias` 专属路由。
  - 用户自选列表优先展示个人别名，全局 `security` 主数据公共名称保持纯洁，彻底消除多租户互相污染。

---

## 2. 变更文件清单

| 文件路径 | 变更类型 | 变更说明 |
|---|---|---|
| `trace/db/migrations/0021_security_and_source_governance.sql` | NEW | 迁移 0021：增加 security.status、watchlist.user_alias 及 source 许可与 Override 字段 |
| `trace/domain/models.py` | MODIFY | 增加 `SecurityStatus`，扩展 `Security`、`Source`、`WatchlistEntry` 模型与属性 |
| `trace/db/repositories.py` | MODIFY | 改造 `SourceRepo`（隔离 override）、`SecurityRepo`（状态生命周期与防冲正）、`WatchlistRepo`（私有别名管理） |
| `trace/common/tickers.py` | MODIFY | 完善 share class 保真解析（`BRK.B` / `BRK-B`），已知交易所映射，新增 `is_valid_ticker_format` |
| `trace/api/schemas.py` | MODIFY | 自选与搜索模型暴露 `status`、`coverage_tier`、`user_alias`，新增 `WatchlistAliasUpdateRequest` |
| `trace/api/routers/watchlist.py` | MODIFY | 修复 Smartbox 截断缺陷，用户别名隔离，自选接口透出分级，新增别名更新 API |
| `trace/api/routers/status.py` | MODIFY | `/ready` 探针迁移数阈值提升至 21 |
| `trace/data/seed_securities.yaml` | MODIFY | 扩充并核验至 46 只核心标的（补齐 QCOM、ASML 官方 CIK 与交易所，明确 verified 状态） |
| `trace/data/seed_sources.yaml` | MODIFY | 登记 29 个数据源的 4 维许可、核验时间与责任人 |
| `trace/data/seed.py` | MODIFY | 适配主数据与数据源新增字段的初始化入库 |
| `docs/coverage_universe.md` | NEW | 正式发布《Trace 覆盖目录与数据源治理白皮书》（3 级架构、46 只标的主数据表、29 个源登记表） |
| `tests/test_universe_and_sources_governance.py` | NEW | T15 专属 8 项全覆盖自动化测试套件 |

---

## 3. 测试验证与演练证据

### 3.1 专属测试套件 (`tests/test_universe_and_sources_governance.py`)
执行结果：**8 passed in 3.39s**
1. `test_zero_dangling_security_map_references`: 验证所有数据源绑定的 security_map 零悬空引用 (Zero Dangling)；
2. `test_source_governance_metadata_and_permissions`: 验证 29 个源具备合规核验元数据，金十数据禁止转发，禁用源许可全关；
3. `test_operational_override_isolation_across_restarts`: 验证手工停用数据源后重启服务执行 seed 加载，手工停用依然生效；清除 override 恢复 seed；
4. `test_candidate_security_status_lifecycle_and_delisted_guard`: 验证动态候选为 `unverified`，退市标的不被冲正，已核验主数据不被降级；
5. `test_user_alias_isolation_zero_pollution`: 验证用户设置个性化自选别名及更新别名时，公共上市公司名称未发生任何改变；
6. `test_share_class_and_exchange_fidelity`: 验证 `BRK.B` / `BRK-B` 完整保留，`TSM -> NYSE` 正确映射，非法代码正确拦截；
7. `test_watchlist_search_and_list_exposes_governance_tier`: 验证自选列表与搜索 API 完整透出 `status` 与 `coverage_tier`（`realtime_monitored`、`context_universe`、`unverified_candidate`）；
8. `test_coverage_universe_doc_consistency`: 验证白皮书与种子数据 46 只标的、29 个数据源 100% 对应。

### 3.2 备份与恢复演练 (`scripts/restore_drill.py`)
- 执行结果：**PASS** (0.158s)
- 包含迁移 0021 的完整 SQLite 数据库通过热备份与沙盒恢复比对，21 项迁移与所有核心表数据量 100% 一致，无任何残缺。
