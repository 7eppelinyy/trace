# Product hardening execution

Goal started 2026-09-20. Scope: independent review R01–R10 / RV01–RV14 and the engineering gaps listed there. Work continues until local engineering acceptance is complete; external user observation, platform approval and live-device validation remain explicit external gates.

## Baseline

- Working tree preserved in `.acceptance/product-hardening-20260920-before.zip` and takeover snapshot `.acceptance/product-hardening-takeover-20260921111704.zip`.
- Manifest and baseline: `verification/product-hardening/next-pass/source-manifest.json` and `baseline.md`.
- Docker runtime is not installed on this host. Container verification is tracked as an explicit external requirement.
- Current latest SQL migration is 0031 (`0031_source_governance_authorization.sql`).

## Execution sequence (Trace_后续修正精确执行计划_2026-09-21.md)

- [x] **N00** 固定接手状态，记录基线与快照
- [x] **N01** 修复测试时钟并恢复全量基线 (P0, 408 passed, 2 Node tests passed)
- [x] **N02** 会话续期与账户/缓存隔离 (P0, 迁移 0028, 令牌轮换与防重放, 缓存隔离)
- [x] **N03** 作业、投递与复核操作闭环 (P0, 迁移 0029, 双进程租约竞争, 租约续期, 单实例锁, Ambiguous处置与审计, 421 passed)
- [x] **N04** 问答证据与模型调用验收 (P0/P1, 统一 LLM 预算, 多进程竞争, 事实硬约束与语义局限性实证, 439 passed)
- [x] **N05** 行情展示与查询延迟隔离 (P1, 迁移 0030, Quote 契约标准化, /events 慢行情解耦, 10万数据复合索引P95<1ms, 445 passed)
- [x] **N06** 修订、向量及独立评测 (P1, 同文档重放保留两份RawItem, Pipeline超72h历史修订穿透, 否认撤回恢复状态机, 文本变更向量失效重算, Label-blind 评测保真, 452 passed)
- [x] **N07** 来源策略与所有发送旁路 (P0/P1, 迁移 0031, 来源技术四能力与合规授权清晰隔离, 拦截探针/流水线/展示/外发/回放/Bot所有旁路, 465 passed)
- [x] **N08** 研究页面与真实用户动作闭环 (P1, 投研全链路闭环, 证据关联与显示许可核验, 并发409乐观锁, 正式limit/offset分页与真total, 私密笔记脱敏限时分享, Telegram/小程序偏好双向统一, 473 passed)
- [x] **N09** 有数据的旧库迁移与恢复 (P0, 0022含关联数据平滑升级至0031, 0025表重建无损, 旧预测legacy_unverified隔离, 基准严格时间校验, db_admin备份恢复与特异路径支持, 全量表SHA256哈希比对一致, RTO 0.111s, 480 passed)
- [x] **N10** 进程,依赖、CI 与容器验证 (P1, requirements-core.lock哈希锁定, 双Node测试纳入CI, 双进程同库长跑验证, /live /ready /status三级探针体系, 资源生命周期释放, 诚实登记Docker本地未安装)
- [x] **N11** 逐需求复核、文档纠偏与最终证据包 (P0, T01-T16与RV01-RV14五维状态验收矩阵, 历史报告打标修订, Truth-in-engineering规范声明)
- [x] **N12** 外部条件登记与真实用户观察 (G0–G3 保持门禁, G0通过, G1-G3严格保持Pending, 梳理微信/法务/留出集/异地灾备/真实用户5大条件执行标准)

## Current status

- **N00 至 N12 全流程 100% 阶段收口完毕**。
- 本地自动化质量底座：Python 测试 480 项全量通过（0 挂起、0 失败）；Node.js 测试 2 项通过；双进程长跑实证通过。
- 交付与证据中心已固化于 `verification/product-hardening/next-pass/` 目录。
- 外部未具备的商业条件（微信 AppSecret、商业源授权、真实模型盲测、异地备份、10 位用户双周观察）已作为标准前置门禁在 `remaining-external-gates.md` 正式登记交接。
