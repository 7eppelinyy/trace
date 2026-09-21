# Trace 需求验收与工程交付复核矩阵 (N11)

> 报告基准: 蓝图 `docs/Trace_产品审阅与Agent执行蓝图_2026-09-19.md` (T01–T16) 与 独立复核 `verification/independent-review-2026-09-20/FULL_INDEPENDENT_REVIEW_REPORT.md` (RV01–RV14)
> 审计日期: 2026-09-21
> Git Commit: `f27252b93d6328eecafa62f6a23a88b46dd54628` (含本次硬化工作区增量)
> 迁移版本集合: 0001 至 0031（共 31 份有序连续 SQL，无空洞）
> 评测数据集 SHA256: `85664602b4478ffec723446f553ef80cee4725fddd9c1a4bd4afd23e79930cf2` (`tests/fixtures/event_pairs_100.json`)
> 自动化测试全量结论: **480 passed, 1 warning (100% 通过)**; Node.js 测试: **2/2 passed**

---

## 1. 五维状态定义说明

每个需求条目必须严格按照以下客观状态标记，杜绝“因为写了代码所以已完成”的模糊推导：
- **[已实现]**: 代码逻辑已落地，参数与分支完备；
- **[自动验证通过]**: 在本地锁定环境（Python 3.11 / Node 24）下通过确定性单元与契约测试；
- **[需要环境验证]**: 本地无硬件/系统环境（如 Docker runtime、真机尺寸测试），需在目标环境运行；
- **[需要外部条件]**: 依赖商业授权文件、微信小程序企业主体与线上 AppSecret、真实模型外网环境或长期真实用户观察；
- **[未完成]**: 逻辑缺失或存在阻断缺陷。

---

## 2. 原始蓝图 16 大任务 (T01–T16) 复核矩阵

| 需求编号 | 需求名称与核心防线 | 对应实现文件 / 核心函数 | 验证测试 / 证据文件 | 状态 | 说明与真实现状 |
|:---:|---|---|---|:---:|---|
| **T01** | 真实渲染与零伪造行情保真 | `miniprogram/pages/detail/detail.js`, `miniprogram/utils/format.js` | `miniprogram/tests/detail_mapping.test.js`, `tests/test_api.py` | 已实现 / 自动验证通过 / 需要环境验证 | 彻底移除 ±1.5% 假行情与 Apple NAND 伪兜底；缺失行情时空态渲染；手机真机跨端待扫码验证 |
| **T02** | 服务端会话鉴权与权限隔离 | `trace/api/security.py`, `trace/api/routers/auth.py`, `0028_session_refresh_lifecycle.sql` | `tests/test_auth_lifecycle.py`, `miniprogram/tests/session_research.test.js` | 已实现 / 自动验证通过 / 需要外部条件 | 设备访客认证与微信 code 登录通道完备，支持 token 自动轮换与重放家族撤销；微信线上登录需 AppSecret 外部凭据 |
| **T03** | Stage B 任务持久化与断点接续 | `trace/db/jobs.py`, `trace/pipeline.py`, `0025_durable_versions_and_leases.sql` | `tests/test_processing_recovery.py`, `tests/test_durable_operational_loop.py` | 已实现 / 自动验证通过 | 多进程排他租约竞拍，租约自动续期，进程崩溃后待重试任务自动恢复，幂等消重 |
| **T04** | LLM 预算硬边界与跨日隔离 | `trace/ai/budget.py`, `trace/ai/llm_client.py`, `0024_llm_usage_scope.sql` | `tests/test_llm_budget.py`, `tests/test_budget_concurrency.py` | 已实现 / 自动验证通过 | 自由文本调用预扣预算，结构化与问答隔离，UTC 零点原子跨日重置；彻底移除“预算耗尽自动降级”假通过 |
| **T05** | 金融归因问答与证据硬约束 | `trace/ai/grounded_answer.py`, `trace/alerts/ask.py` | `tests/test_ask_grounding.py`, `tests/test_grounded_answer_hardened.py` | 已实现 / 自动验证通过 | 证据逐字引用检验，无证据诚实拒答，禁止注入 prompt 突破范围；外部模型评估需独立标注留出集 |
| **T06** | Outbox 可靠投递与幂等保障 | `trace/db/outbox.py`, `trace/alerts/delivery_worker.py`, `0029_outbox_audit_and_management.sql` | `tests/test_outbox.py`, `tests/test_n03_durable_jobs_and_outbox.py` | 已实现 / 自动验证通过 | 租约竞争、网络超时判定为 ambiguous 隔离并提供 CLI/API 管理处置审计；恢复旧库自动 quarantine 杜绝重复告警 |
| **T07** | 依赖锁与断网测试隔离环境 | `requirements-core.lock`, `Dockerfile`, `tests/conftest.py` | `verification/product-hardening/next-pass/pytest-full.txt` | 已实现 / 自动验证通过 | `--require-hashes` 锁定 100% 依赖；测试套件默认阻断真实外网请求，使用显式受控时钟夹具 |
| **T08** | 事件时间线与事实演化呈现 | `miniprogram/pages/index/index.js`, `miniprogram/pages/detail/detail.js` | `miniprogram/tests/detail_mapping.test.js`, `tests/test_api.py` | 已实现 / 自动验证通过 / 需要环境验证 | 事实/不确定性/核验点结构化映射完成；手机真机视觉待微信开发者工具实测 |
| **T09** | 语义聚类、误合并拆分与修订 | `trace/event_engine/semantic_cluster.py`, `trace/event_engine/revision.py` | `tests/test_n06_revision_vectors_and_evaluation.py`, `N06-revision-vectors-eval.md` | 已实现 / 自动验证通过 / 需要外部条件 | 官方否认/撤回状态机运转正常；100 对金标完成 label-blind 评测（召回率 1.0，30 自动合并，70 待审核）；生产模型泛化度需留出集 |
| **T10** | 时区敏感早报与用户偏好持久化 | `trace/alerts/digest.py`, `trace/api/routers/preferences.py`, `0017_notification_preferences.sql` | `tests/test_preferences_and_digest.py`, `tests/test_digest_timezone.py` | 已实现 / 自动验证通过 | Telegram 与小程序写偏好统一至同一底层仓储；静音频带跨午夜计算正确；总开关关闭拦截全部推送 |
| **T11** | SQL 定界游标分页与无截断导出 | `trace/common/cursors.py`, `trace/api/routers/events.py`, `0030_event_cursor_and_quote_perf.sql` | `tests/test_pagination_and_query_governance.py`, `tests/test_n08_research_and_user_actions.py` | 已实现 / 自动验证通过 | 彻底消除 N+1 查询与静默 1000 条截断；10 万条数据下 seek cursor P95 查询延迟 < 0.5ms |
| **T12** | 报价质量分级与市场确认解耦 | `trace/collectors/market_data/time_quality.py`, `trace/api/routers/events.py` | `tests/test_quote_quality_and_events_decoupling.py`, `tests/test_market_confirmation.py` | 已实现 / 自动验证通过 | Quote 严格区分日涨跌与 15m 区间涨跌；事件列表异步解耦快读；陈旧/延时/休市行情打标拒绝冒充实时 |
| **T13** | 不可变预测快照与防前视回测 | `trace/feedback/ledger.py`, `trace/db/repositories.py`, `0026_forecast_provenance.sql` | `tests/test_forecast_ledger.py`, `tests/test_immutable_forecast_snapshots.py` | 已实现 / 自动验证通过 | 预测生成时刻（`analysis_created_at`）价格固化；旧记录标记 `legacy_unverified` 隔离；基准超额与个股执行同等新鲜度规则 |
| **T14** | 运维探针分级与本地备份恢复 | `trace/db/backup.py`, `scripts/restore_drill.py`, `trace/api/routers/status.py` | `tests/test_n09_migration_data_and_restore.py`, `restore-proof.json` | 已实现 / 自动验证通过 / 需要环境验证 | `/live`, `/ready`, `/status` 三级解耦；恢复演练全量表 SHA256 比对一致，RTO 0.111s；跨机容灾需外部服务器 |
| **T15** | 标的治理分级与来源许可策略 | `trace/common/source_policy.py`, `0031_source_governance_authorization.sql` | `tests/test_universe_and_sources_governance.py`, `tests/test_n07_source_policy_and_bypasses.py` | 已实现 / 自动验证通过 / 需要外部条件 | fetch/store/display/forward 四大能力位全旁路拦截；撤销授权即刻生效；商业源实际法律授权协议需外部商务签署 |
| **T16** | 私有研究假设与真实反馈闭环 | `trace/api/routers/research.py`, `trace/db/repositories.py`, `0022_hypothesis_tracking_and_feedback.sql` | `tests/test_n08_research_and_user_actions.py`, `N08-research-workflow.md` | 已实现 / 自动验证通过 / 需要外部条件 | 事件生成研究草案、私有假设、新证据关联复核、只读脱敏分享快照、409 乐观锁冲突；付费与留存价值需 10 位真实用户 2 周观察 |

---

## 3. 独立复核 14 项专项缺陷 (RV01–RV14) 闭环状态

| 缺陷编号 | 缺陷描述与整改要求 | 对应代码与处理措施 | 证明材料与测试 | 状态 |
|:---:|---|---|---|:---:|
| **RV01** | 全量回归有时钟依赖失败 | 注入显式测试时钟夹具，保留行情过期与休市的真正负例语义 | `tests/test_immutable_forecast_snapshots.py`, `N01-test-clock.md` | **已闭环** |
| **RV02** | 会话刷新生命周期未走真实接口 | 实现 `/api/v1/auth/session/refresh`，引入 refresh token 轮换与家族重放撤销，客户端缓存按 host/user 双键隔离 | `tests/test_auth_lifecycle.py`, `miniprogram/tests/session_research.test.js`, `N02-session-lifecycle.md` | **已闭环** |
| **RV03** | 任务队列并发与恢复缺乏两进程实证 | 编写独立多进程租约竞争与心跳续租测试，断网/挂起任务自动释放并由备节点接管 | `tests/test_durable_operational_loop.py`, `N03-durable-processing.md` | **已闭环** |
| **RV04** | Outbox ambiguous 状态堆积缺乏运维出口 | 增加 `/admin/outbox/ambiguous` 与 CLI `--list-ambiguous` / `--resolve`，记录操作人与审计日志 | `tests/test_n03_durable_jobs_and_outbox.py`, `trace/db/outbox.py`, `0029_outbox_audit_and_management.sql` | **已闭环** |
| **RV05** | LLM 自由文本调用绕过预算预扣 | 统一至 `reserve_budget`，强制正整数扣减，实现 UTC 跨日原子滚动 | `tests/test_budget_concurrency.py`, `N04-grounded-qa.md` | **已闭环** |
| **RV06** | 问答评测依赖外部外网 API 制造假通过 | 建立本地断网确定性问答规范，验证逐字引用比对与无证据诚实拒答 | `tests/test_grounded_answer_hardened.py`, `N04-grounded-qa.md` | **已闭环** |
| **RV07** | 事件列表加载受慢行情同步阻塞 | 重构 Quote 返回标准契约，异步解耦 `/events`，提供快速查询索引与游标 P95 < 0.5ms | `tests/test_quote_quality_and_events_decoupling.py`, `0030_event_cursor_and_quote_perf.sql`, `N05-market-quotes.md` | **已闭环** |
| **RV08** | 官方撤回/澄清无法更正事件 | 增强语义状态机，支持 official_clarification 转换为 contradicted 状态并保留修订历史 | `tests/test_n06_revision_vectors_and_evaluation.py`, `N06-revision-vectors-eval.md` | **已闭环** |
| **RV09** | 100 对评测存在标签泄露与虚假夸大 | 保持 predict_pair label-blind 契约，如实公布 1.0 召回、30 自动合并、70 需审核的实际表现 | `trace/verification/dedup_evaluation.py`, `N06-revision-vectors-eval.md` | **已闭环** |
| **RV10** | 来源许可政策在展示/转发存在旁路 | 建立 `0031_source_governance_authorization.sql`，严格封堵 `/events`, `/ask`, 草案引用, worker 转发及 Bot 查询全旁路 | `tests/test_n07_source_policy_and_bypasses.py`, `N07-source-policy.md` | **已闭环** |
| **RV11** | 用户研究假设未形成完整使用闭环 | 实现事件创建草案、私有假设、新证据关联、409 乐观锁冲突、脱敏公开分享与 1000+ 条无截断导出 | `tests/test_n08_research_and_user_actions.py`, `N08-research-workflow.md` | **已闭环** |
| **RV12** | 数据库升级缺乏历史业务数据实测 | 构造迁移至 0022 的完整数据临时库，执行 0023-0031 迁移，实证 0025 表重建与外键保全 | `tests/test_n09_migration_data_and_restore.py`, `migration-proof.json`, `N09-migration-and-restore.md` | **已闭环** |
| **RV13** | 备份恢复演练只比对表行数且允许覆盖 | 增加全量 38 张业务表 SHA256 哈希比对；强制新目标路径；增加恢复后 sending 告警隔离 | `tests/test_n09_migration_data_and_restore.py`, `restore-proof.json`, `scripts/restore_drill.py` | **已闭环** |
| **RV14** | 缺少双进程长跑实证与容器脱节 | 运行 `verify_local_deployment.py` 验证 API + Runner 共享库与端口释放；诚实记录 Docker 本地未就绪 | `local-process-smoke.json`, `N10-clean-build-and-pipeline.md` | **已闭环** |

---

## 4. 核心业务语义准确性规范声明 (Truth-in-Engineering)

1. **设备访客认证 vs 微信认证**:
   - `grant_type="guest"` 为纯设备级匿名临时凭据（便于免登预览与合规受控体验），其产生的数据绑定在当前设备；
   - 微信认证（`grant_type="wechat"`）依赖真实微信开放平台线上 AppSecret 换取全局唯一 openid，用于多端同步与消息推送。二者权限明确分离。
2. **研究分享快照 (Capability Snapshot)**:
   - 研究问题的分享通过一次性不可逆令牌生成静态只读快照；
   - 用户的私有备注（`user_notes`）在快照中被强制剔除；
   - 分享快照可由创建者随时撤销，撤销后访问直接返回 404。
3. **Seek Cursor 游标分页语义**:
   - 分页采用 `(first_seen_at, event_id)` 复合字段定界，属于稳定单调排序；
   - 彻底解决了基于 offset 的跳页穿透与深分页性能退化问题，在数据持续写入时不会出现漏条目或重复拉取。
4. **价格背景 vs 事件反应与预测收益**:
   - 行情展示中的 `change_pct_day` 仅表示证券当日大盘行情背景；
   - 预测核对使用的是严格从预测生成时刻 `analysis_created_at`（`anchor_price`）至到期时刻 `due_at` 的事件区间收益，两者在数据结构与展示界面严格分离。
5. **历史预测 Legacy 排除**:
   - 0026 迁移将历史不可证明时点真实性的预测标记为 `legacy_unverified`；
   - 该部分数据在 SQLite 中永久保存备查，但绝不计入 `/status` 和 `/accuracy` 的命中率（`hit_rate`）和实测率（`measurement_rate`）分母。
