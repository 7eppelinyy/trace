# Trace 外部门禁清单与投产交接管理台账 (N12)

> 报告基准: `docs/Trace_后续修正精确执行计划_2026-09-21.md` 第 15 节 (N12)
> 审计原则: **G1/G2/G3 门禁绝不降级改定义；没有外部客观证据前保持 PENDING；严禁以单元测试假冒生产投产。**
> 责任交接人: 工程集成负责人 / 产品运营负责人 / 法务与合规负责人

---

## 1. 门禁定义与当前状态总览

| 门槛代号 | 门槛标准定义 | 当前工程就绪状态 | 外部条件与当前状态 | 最终发布签署结论 |
|:---:|---|---|---|:---:|
| **Gate G0** | **可信演示**: 零伪造数据、零死假曲线、无网络时诚实报错。 | 自动化契约测试 100% 通过；伪数据完全清除。 | 本地及模拟网络环境均已满足。 | **签署通过 (PASS)** |
| **Gate G1** | **受控内测**: 真实身份鉴权、异常恢复、配额硬控制、无无界资源泄漏。 | 会话轮换、Stage B 恢复、预算预约锁、Outbox 审计均已交付。 | 缺微信生产 AppSecret、缺 Docker 运行环境。 | **待外部条件 (PENDING)** |
| **Gate G2** | **可重复研究闭环**: 修订更正流、事件事实演进、真实行情质量与多日稳定运行。 | 官方否认状态机、seek cursor、Quote 异步解耦、旧库迁移无损。 | 缺 48 小时多节点持续运行观察与真实模型留出集评测。 | **待外部条件 (PENDING)** |
| **Gate G3** | **商业投产试验**: 商业源授权闭环、灾难异地恢复、真实用户价值与付费留存验证。 | 来源权限拦截、脱敏分享快照、SHA256 恢复演练完成。 | 缺商业源书面法务授权、缺 10 位真实用户 2 周观测。 | **待外部条件 (PENDING)** |

---

## 2. 五大外部条件登记与交接表

### 外部条件 1：微信小程序真实身份、合法域名与真机环境
- **对应门槛**: Gate G1 (受控内测)
- **当前状态**: `PENDING (WAITING_WECHAT_CREDENTIALS)`
- **需要谁提供什么**:
  - 产品/运营提供：微信开放平台主体企业认证、小程序 AppID 与生产 AppSecret；
  - 运维提供：已备案的生产 HTTPS API 域名（配置于微信公众平台 request 合法域名白名单）。
- **本地已就绪工程资产**:
  - `miniprogram/` 全套前端源码已接入 `/api/v1/auth/session`，支持微信 `code` 与设备访客双模式；
  - `miniprogram/tests/session_research.test.js` 自动化测试通过；
  - 微信开发者工具项目配置（`project.config.json`）已配置完毕。
- **线上执行步骤**:
  1. 在微信公众平台后台将生产 API 域名填入“开发管理 -> 开发设置 -> 服务器域名”；
  2. 运维在生产环境变量中配置 `WECHAT_APP_ID` 与 `WECHAT_APP_SECRET`；
  3. 运营使用微信开发者工具上传版本至“体验版”；
  4. 至少使用 1 台 iPhone（iOS 16+）与 1 台 Android（Android 12+）手机进行真机扫码登录、研究假设保存、图谱渲染与分享操作。
- **证据存放位置**: `verification/external-gates/G1-wechat-production.json`（待建）
- **通过标准**: 真实微信账号扫码可顺利完成身份签发、多端研究数据同步，不产生任何 502/SSL 错误。

---

### 外部条件 2：数据源与行情 API 真实商业授权许可
- **对应门槛**: Gate G3 (商业发布)
- **当前状态**: `PENDING (WAITING_LEGAL_LICENSE)`
- **需要谁提供什么**:
  - 法务/商务负责人提供：金十数据（Jin10）、SEC EDGAR、Alpaca、腾讯行情（或同花顺/聚宽等代用源）的书面许可协议编号、授权生效日期与可存储/分发范围说明文件。
- **本地已就绪工程资产**:
  - `0031_source_governance_authorization.sql` 来源治理仓储已上线；
  - `SourceAuthorizationRepo` 支持记录 `license_type`, `can_store`, `can_display`, `can_forward`, `verified_by`, `expires_at`；
  - 全流水线 4 个能力位（fetch/store/display/forward）旁路拦截机制经过 13 项自动化测试（`test_n07_source_policy_and_bypasses.py`）实证。
- **线上执行步骤**:
  1. 法务核验实际采购协议；
  2. 管理员调用 API `POST /api/v1/admin/sources/{id}/authorize` 录入正式许可编号、范围与核验人；
  3. 系统自动解除受限来源的阻断标记。
- **证据存放位置**: `verification/external-gates/G3-commercial-source-licenses.md`（待建）
- **通过标准**: 所有开启商业分发的金融来源均具备对应的法律授权证书且在有效期内，未授权数据源被物理阻断且无旁路。

---

### 外部条件 3：真实模型对抗注入评测与独立留出集表现
- **对应门槛**: Gate G2 (质量与鲁棒性)
- **当前状态**: `PENDING (WAITING_HELD_OUT_DATASET)`
- **需要谁提供什么**:
  - 算法/评测专家提供：不重叠于训练集与测试集（`tests/fixtures/event_pairs_100.json`）的至少 200 条全新金融新闻/公告配对独立盲测标注集；
  - 安全团队提供：针对 Prompt 注入与越狱尝试的攻击用例集。
- **本地已就绪工程资产**:
  - Label-blind 评测接口 `trace/verification/dedup_evaluation.py`；
  - 确定性结构化证据问答过滤器 `trace/ai/grounded_answer.py`。
- **线上执行步骤**:
  1. 运行 `python -m trace.verification.dedup_evaluation --dataset <held_out_path>`；
  2. 评测真实模型在全新事件上的召回率、准确率与误合并拆分成本；
  3. 执行 Prompt 越狱压力测试，验证是否严格保持逐字证据约束。
- **证据存放位置**: `verification/external-gates/G2-model-held-out-evaluation.json`（待建）
- **通过标准**: 独立盲测集合上自动合并错误率 < 1%，对抗攻击下未引用证据答复率 0%。

---

### 外部条件 4：异地容灾存储与独立实例恢复
- **对应门槛**: Gate G1 / Gate G2
- **当前状态**: `PENDING (WAITING_OFFSITE_STORAGE)`
- **需要谁提供什么**:
  - 云基础架构运维人员提供：S3/OSS 异地只读副本存储桶权限与独立目标演练机（与生产机器不同规格或不同 VPC）。
- **本地已就绪工程资产**:
  - `scripts/db_admin.py`（支持安全参数传递、特殊路径支持、`--quarantine-outbox` 防重复告警）；
  - `scripts/restore_drill.py`（38 张业务表 SHA256 完整性哈希校验，本地 RTO 0.111s）。
- **线上执行步骤**:
  1. 配置定时任务将 `data/backups/trace-*.db` 异步同步至云端隔离存储桶；
  2. 在冷备演练机上拉取备份文件，执行 `python scripts/db_admin.py restore --source <s3_backup> --target <new_db> --quarantine-outbox`；
  3. 启动 API 服务，核验 `/ready` 探针与关键业务表哈希一致性。
- **证据存放位置**: `verification/external-gates/G1-offsite-restore-log.json`（待建）
- **通过标准**: 异地灾难恢复全流程 RTO < 5 分钟，数据 RPO < 24 小时（或备份周期时长），所有业务表哈希完全匹配。

---

### 外部条件 5：目标用户群（约 10 人）双周跟踪观察与留存价值闭环
- **对应门槛**: Gate G3 (付费试验门槛)
- **当前状态**: `PENDING (WAITING_2WEEK_USER_STUDY)`
- **需要谁提供什么**:
  - 产品经理与研究员提供：招募 10 位具备半导体/硬科技投资背景的分析师或个人投资者，进行为期至少 14 天的日常跟踪使用。
- **本地已就绪工程资产**:
  - 研究假设工作台（`miniprogram/pages/research/` 与 `/api/v1/research/`）；
  - 提醒有效性反馈机制（`alert_feedback` 仓储与 API）；
  - 研究资产全量导出（`export_user_questions` 1000+ 条无截断支持）。
- **观察步骤与数据采集**:
  1. 用户在真实事件中创建个人私有研究假设（例如晶圆代工排产、订单修正）；
  2. 观察到达新证据时，系统能否正确提示“新证据到达待复核”；
  3. 统计 14 天内用户主动更新假设状态、标记反馈（useful / not_useful）的留存频次；
  4. 访谈用户是否切实节省了跨媒体手动追踪时间。
- **证据存放位置**: `verification/external-gates/G3-user-cohort-study-report.md`（待建）
- **通过标准**: 至少 70% 参与用户完成至少 1 条完整假设建立至证伪/归档全周期，有效提醒率（useful / 总评价）>= 60%。

---

## 3. 签署结论与后续行动清单

1. **当前系统本地工程已 100% 准备就绪**:
   - 480 项 Python 自动化测试、2 项 Node.js 测试、双进程同库实证、历史迁移及灾备演练脚本全部通过。
2. **生产准入签署状态**:
   - **Gate G0**: **签署通过 (PASS)**
   - **Gate G1 / G2 / G3**: 依据客观真实性原则，**在外部凭据与用户观察就绪前严格保持 PENDING**。
3. **后续推进建议**:
   - 第一优先级: 商务法务签署商业数据源授权，并由运维配置微信 AppSecret；
   - 第二优先级: 部署至预发服务器接入 Docker Compose，执行跨机恢复与 48 小时流水线监控；
   - 第三优先级: 启动 10 位首批邀请制用户双周受控内测。
