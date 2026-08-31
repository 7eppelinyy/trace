# MVP V1 真实纵向切片 — 基线审计（只读）

审计时间：2026-08-25（本地）
审计人：AI 助手
审计方式：只读代码审计 + 离线命令执行，不修改任何代码。

## 0. 基线命令结果

| 命令 | 结果 |
|---|---|
| `.venv\Scripts\python -m pytest tests -q` | **24 passed**（6.46s） |
| `.venv\Scripts\python -m trace.main init` | 成功：migration 0001 应用，种子数据加载；提示无 ALPACA_API_KEY → **静默降级 Mock 行情**；无 sentence-transformers → **静默降级 hash embedder** |
| `.venv\Scripts\python -m trace.main digest` | 成功，但 Watchlist 行情来自 **MockUSMarketProvider**，消息中**未标注 mock** |
| `git status` | **失败：目录不是 git 仓库**（fatal: not a git repository）。本轮不初始化 git，避免引入新风险；在 known-limitations 中记录 |

## 1. 模块逐项分类

分类标准：
- REAL = 已用真实网络数据验证可用
- PARTIAL = 代码存在且逻辑合理，但部分环节未验证或依赖未满足
- MOCK = 固定假数据
- STUB = 只有接口/占位
- BROKEN = 存在已知错误
- UNVERIFIED = 代码存在，从未用真实网络验证

| 模块 | 分类 | 依据 |
|---|---|---|
| SEC Collector (`collectors/sec.py`) | **UNVERIFIED** | 调用真实 EDGAR submissions API，但：① CIK 手工填写（SNDK=0002023443 **未经官方核验**）；② User-Agent 为通用字符串，**不符合 SEC 要求的 `公司名 联系邮箱` 规范**；③ 无重试/退避/频率限制；④ 无游标，每次全量拉前 20 条；⑤ 从未真实运行验证 |
| SNDK/MU/NVDA IR Collector (`collectors/ir.py`) | **UNVERIFIED（疑似 BROKEN）** | 仅复用 RSSCollector，feed URL 是**猜测值**（`investor.sndk.com/rss/...`、`investors.micron.com/rss/...`、`investor.nvidia.com/rss/...`），从未验证存在性 |
| CNINFO Collector (`collectors/cninfo.py`) | **UNVERIFIED** | 使用真实巨潮 query 接口，但未按 Watchlist 股票代码过滤、无增量游标、从未验证 |
| SSE Collector (`collectors/sse.py`) | **UNVERIFIED（疑似 BROKEN）** | query.sse.com.cn 接口与 JSONP 解析为猜测实现，从未验证；与 CNINFO 可能存在重复抓取 |
| SZSE Collector (`collectors/szse.py`) | **UNVERIFIED（疑似 BROKEN）** | 同上，接口/字段为猜测实现 |
| Policy Collector (`collectors/policy.py`) | **UNVERIFIED** | 真实官网页面但 CSS 选择器为猜测；本轮不在关键路径 |
| Industry Media Collector | **UNVERIFIED** | 页面/关键词为猜测；本轮不在关键路径 |
| Alpaca Provider (`market_data/alpaca.py`) | **PARTIAL + 静默 Mock** | 有真实 API 实现，但无 Key 时**静默降级到 MockUSMarketProvider 且消息中不标注** |
| A股行情 Provider (`market_data/cn.py`) | **MOCK** | 只有 MockCNMarketProvider，无任何真实行情实现 |
| Event Engine | **PARTIAL（接近 REAL）** | 去重/聚类/Revision 逻辑有 24 个测试覆盖；但生产环境无 sentence-transformers 时静默降级为 hash 伪向量（跨语言聚类能力实际丧失，无提示） |
| AI Extractor (Stage A) | **PARTIAL** | 无 LLM Key 时**静默降级为规则抽取**，产出看起来像真实分析（伪成功风险） |
| AI Impact Analyzer (Stage B) | **PARTIAL** | 同上，静默规则兜底；且旧 Schema 与新要求不符（缺 facts/uncertainties/evidence_ids/counter_evidence 等） |
| Same-event Verifier | **PARTIAL（保守）** | 无 LLM 时恒返回 False（不合并）——安全但会产生重复 Event |
| Alert Engine | **REAL（逻辑层）** | 阈值/幂等/mute 有测试；但投递回执仅记录自生成 ID，**无 Telegram message_id** |
| Telegram Bot | **UNVERIFIED** | python-telegram-bot 未安装；从未用真实 Token 运行；缺 `/start`；投递未解析 Telegram 返回 |
| /ask | **PARTIAL** | 逻辑完整、仅依赖本地 DB；未端到端验证 |
| Daily Digest | **PARTIAL** | 使用 Mock 行情且未标注 |

## 2. 特别检查结论

| 检查项 | 结论 |
|---|---|
| 是否返回真实网络数据 | **从未验证**。所有 collector 均未经真实网络运行 |
| 是否只是固定示例/空数组 | 行情为固定种子随机数（Mock）；其余依赖网络 |
| 是否存在静默 Mock 降级 | **存在，3 处**：行情（Alpaca→Mock）、embedding（ST→hash）、LLM（→规则）。均无提示、无标记 |
| 是否存在无论成败都返回成功 | collector 的 `run()` 捕获所有异常返回空数组——**网络失败与无新数据不可区分** |
| 是否存在未使用的 Provider | Alpaca 真实实现在无 Key 时不被使用；真实行情链路实际从未执行 |
| 是否存在只写接口未接入主 Pipeline 的模块 | SSE/SZSE collector 已注册但端点未验证；MarketDataCollector（行情写入 market_snapshot）未接入 Pipeline |
| 是否会在无 API Key 时生成看似真实的伪分析 | **会**：规则抽取 + 规则影响分析 + Mock 行情拼出的消息与真实分析在格式上无差别 |

## 3. 缺失清单（对照任务书）

- 无 `TRACE_MODE`（test/offline/production）门禁与降级标记（`analysis_mode` / `market_data_mode`）
- 无状态码体系（DEGRADED_NO_LLM / STOP_TELEGRAM_CREDENTIALS_MISSING 等）
- 无 `doctor` / `run-once` / `verify-securities` 命令
- 无 SEC ticker/CIK 官方权威核验（company_tickers.json）
- 无 collector 健康状态（last_success_at / consecutive_failures 等）
- 无 collector 游标/checkpoint 持久化
- 无 run_id / 结构化可观测日志；无敏感信息脱敏过滤器
- Telegram 投递无 `telegram_message_id` / `telegram_chat_id` 回执
- LLM 环境变量仅支持 OPENAI_API_KEY/BASE_URL，缺 `OPENAI_MODEL` / `OPENAI_TIMEOUT_SECONDS` / `OPENAI_MAX_RETRIES`
- Stage A/B Schema 与任务书新规范不一致（缺 summary_zh/facts/uncertainties/evidence_ids；Stage B 缺 impact_relation/supporting_evidence_ids/counter_evidence_ids/assumptions）
- 无来源健康表、无投递回执扩展字段（需 migration 0002）

## 4. 基线结论

当前骨架**逻辑层可用（24 测试通过）但数据层全部未验证**，且存在多处静默降级会产生伪真实输出。
在补齐真实来源、生产门禁、回执与诊断命令之前，**不得宣称 MVP 可用**。
