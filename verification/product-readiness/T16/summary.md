# T16 交付总结：假设跟踪与真实用户验证 (F31/F32)

> **任务编号**：T16  
> **优先级**：P2 (Gate G3 付费试验与真实用户价值验证前置)  
> **关联需求**：F31 (假设跟踪工作台与新证据匹配)、F32 (提醒有效性原因反馈闭环)、Gate G3 审计标准  
> **交付日期**：2026-09-19  

---

## 1. 核心需求与设计实现

依据蓝图第 3、7 节及验收标准，T16 为产品从“资讯推送”升级为“跨市场产业事件的证据与跟踪工作台”的关键闭环：

### 1.1 假设跟踪与条件化推导 (F31)
- **独立工程分层**：设立专门的 API 路由 `trace/api/routers/research.py` 与专属仓储，杜绝将研究动作混杂进 `ask.py`。
- **假设草案智能提取** (`POST /api/v1/research/questions/draft`)：
  - 基于传入 `event_id` 与可选的 `security_id`，提取事件核心事实并结合 `EventImpact` 传导逻辑；
  - 自动预填标题、假设陈述、支持条件列表（如龙头订单落实、财报毛利率改善）、反证条件列表（官方否认、替代方案填补、行情无反应）以及首个核验观察窗口（`next_check_at`）。
- **假设生命周期管理**：
  - 状态流转：`tracking`（持续跟踪） -> `confirmed`（证实成立） / `falsified`（反证证伪） / `archived`（已归档）；
  - 支持按状态（`state`）与标的（`security_id`）分页/列表筛选。
- **关联证据沉淀与半自动匹配** (`match_new_evidence` & `POST /questions/{id}/evidence`)：
  - 新到达的原始证据如果关联当前跟踪假设的事件或标的，自动打上“可能相关，需要复核”标记，并写入 `matched_evidence_ids`；
  - 核心判断与状态推进完全交由用户确认，系统绝不越俎代庖替用户做事实断言。

### 1.2 多租户隔离与私有备注防泄露门禁 (F31 / Gate G3)
- **严格对象级鉴权**：
  - 用户只能查看、更新、删除属于自己的研究假设与私密备注；
  - 试图越权操作其他用户的假设时，接口严格返回 `403 Forbidden`，列表查询具备天然租户隔离。
- **公开分享脱敏机制** (`GET /api/v1/research/questions/{id}/share`)：
  - 允许向外界公开分享结构化假设与推断条件（`ResearchQuestionShareResponse`）；
  - **核心约束**：个人私有备注（`user_notes`）在数据结构和序列化响应中被彻底剔除，杜绝个人敏感操作记录、仓位笔记或私密观点泄露到公开网络。

### 1.3 提醒有效性与原因反馈闭环 (F32)
- **多维反馈采集** (`POST /api/v1/research/feedback`)：
  - 覆盖用户对系统推送提醒的真实体验评价：`useful`（有用）、`not_useful`（无用）、`irrelevant`（与我不相关）、`too_late`（提醒太晚/已反映在行情中）、`incorrect_analysis`（分析判断错误）、`duplicate`（重复推送）；
  - 支持用户填写详细原因文本，为模型微调与规则优化沉淀第一手评测数据。
- **聚合统计报表** (`GET /api/v1/research/feedback/summary`)：
  - 支持查看个人（`scope=my`）以及全系统（`scope=all`）的提醒有效性指标：总条数、有效条数、无效条数、有效转化率（`useful_rate`）与各项原因的频次分布。

### 1.4 可移植研究资产导出 (Gate G3)
- **用户资产无锁定保障** (`GET /api/v1/research/export`)：
  - 用户可随时以标准化 JSON 格式一键导出自己创建的所有研究假设（含完整的支持/反证条件、新证据关联、私有笔记）以及所有提醒反馈记录；
  - 满足 Gate G3 要求：“即使停止实验，用户可导出自己的研究内容”。

---

## 2. 变更文件清单

| 文件路径 | 变更类型 | 变更说明 |
|---|---|---|
| `trace/db/migrations/0022_hypothesis_tracking_and_feedback.sql` | NEW | 迁移 0022：创建 `research_question` 与 `alert_feedback` 核心表及高效查询索引 |
| `trace/common/ids.py` | MODIFY | 新增 `research_question_id()` (`RQ-`) 与 `alert_feedback_id()` (`AFB-`) 生成函数 |
| `trace/domain/models.py` | MODIFY | 新增 `ResearchQuestionState` 枚举、`ResearchQuestion` 与 `AlertFeedback` 领域模型 |
| `trace/domain/__init__.py` | MODIFY | 统一导出新领域模型 |
| `trace/db/repositories.py` | MODIFY | 新增 `ResearchQuestionRepo`（全生命周期与新证据匹配）与 `AlertFeedbackRepo`（反馈写入与聚合统计） |
| `trace/api/schemas.py` | MODIFY | 增加假设草案、增删改查、公开脱敏分享、反馈采集与可移植导出等 9 个 Pydantic 模式 |
| `trace/api/routers/research.py` | NEW | 研发与用户验证专属 API 路由集合（10 个标准化端点） |
| `trace/api/app.py` | MODIFY | 挂载 `/api/v1/research` 路由 |
| `trace/api/routers/status.py` | MODIFY | `/ready` 服务就绪探针全量迁移版本门禁升级至 22 |
| `tests/test_hypothesis_and_user_validation.py` | NEW | T16 专属 6 大场景自动化集成与安全测试套件 |

---

## 3. 测试验证与演练证据

### 3.1 专属集成测试 (`tests/test_hypothesis_and_user_validation.py`)
执行命令：`.\.venv\Scripts\python.exe -m pytest -v tests/test_hypothesis_and_user_validation.py`  
测试结果：**6 passed in 3.11s**
1. `test_unauthenticated_research_endpoints_rejected`: 验证未认证请求访问研究与反馈接口严格返回 401；
2. `test_hypothesis_draft_generation`: 验证基于事件与传导影响自动推导假设草案、支持/反证条件与核验窗口；
3. `test_hypothesis_lifecycle_and_evidence_matching`: 验证创建跟踪、列表检索、关联新证据、推进状态至 confirmed 及删除操作完整闭环；
4. `test_tenant_isolation_and_public_share_desensitization`: 验证越权访问拦截（403 Forbidden），且公开分享接口严格脱敏剥离 `user_notes`；
5. `test_alert_feedback_and_summary_aggregation`: 验证有用/太晚/无关等反馈录入及个人/全局聚合有效率与分布统计；
6. `test_portable_research_export`: 验证用户随时可完整导出自己名下的假设与反馈资产，且不包含他人数据。

### 3.2 就绪探针与系统健康 (`tests/test_health_and_deployment.py`)
执行命令：`.\.venv\Scripts\python.exe -m pytest -v tests/test_health_and_deployment.py`  
测试结果：**7 passed in 2.90s**
- `/ready` 探针成功识别 22 项迁移完整应用，`schema_status: up_to_date`。

### 3.3 灾难恢复演练 (`scripts/restore_drill.py`)
执行命令：`.\.venv\Scripts\python.exe scripts/restore_drill.py`  
演练结果：**PASS (0.124s)**
- 包含迁移 0022 的完整 SQLite 生产数据库在线一致性热备并沙盒恢复，所有表（包含 `research_question` 与 `alert_feedback`）行数、外键及结构完整性校验 100% 吻合。
