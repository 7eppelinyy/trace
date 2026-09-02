# Trace — 美股 + A股重大事件智能雷达

基于**官方一手来源**（SEC EDGAR / 公司 IR / 巨潮 / 官方政策站点 / 已确认官方 feed 的产业媒体）
的重大事件采集 → 跨语言事件聚类 → LLM 结构化分析 → 集中评分 → Telegram 预警系统。

完整规划见《美股+A股重大事件智能雷达与 Telegram 预警系统——Phase 1 实施规划书.md》；
各阶段验收证据在 `verification/`。

## 架构总览

```
trace/
├── collectors/          # 数据源采集器（SEC / IR / SanDisk / Micron / 巨潮 / SSE/SZSE 适配器 / 政策 / RSS / 产业媒体）
│   └── market_data/     # 行情 Provider（Alpaca / A股占位 / 市场确认）
├── event_engine/        # 去重（确定性 hash → 语义聚类 → LLM verifier）+ Event 修订
├── ai/                  # Stage A 事件抽取 / Stage B 影响分析 / same-event verifier / LLM 客户端
├── graph/               # 产业链图谱（SQLite 邻接表 + BFS）
├── scoring/             # 集中评分引擎 + 供需景气确定性信号
├── alerts/              # 阈值/幂等/首次同步保护 + Telegram 模板 + 每日摘要 + /ask
├── feedback/            # 预测回测账本：方向预测 vs 事后真实行情（/accuracy 查看）
├── bot/                 # Telegram Bot 命令 + 投递回执
├── db/                  # SQLite(WAL) + Repository 层 + migration + source_health + 每日备份轮换
├── market_time/         # 跨市场交易日历
├── common/              # http 客户端(重试/退避/限流) / hashing / 模式门禁 / 可观测性
├── pipeline.py          # run-once / 长驻循环编排
├── app.py               # Composition Root（AppContext 装配）
├── main.py              # CLI 入口
├── config.py            # settings.yaml + .env 加载
└── settings.yaml        # 所有阈值/权重/间隔集中配置
```

数据流：`采集 → Level 1 确定性去重 → Stage A 抽取 → Level 2/3 语义聚类 →
Event 创建/修订 → Stage B 影响分析（图谱候选）→ 集中评分（+行情确认+供需信号）→
Alert 评估（阈值/幂等/静音/新鲜度）→ Telegram 投递（解析回执）`

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # 填入 GEMINI_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_DEFAULT_CHAT_ID 等

python -m trace.main init             # 初始化数据库 + 种子数据
python -m trace.main doctor           # 环境诊断（不泄露凭据）
python -m trace.main run-once         # 单次真实流水线（验收用）
python -m trace.main bot              # Telegram Bot（long polling）+ 流水线长驻循环
python -m trace.main run              # 仅流水线循环（systemd 部署用）
python -m trace.main digest           # 生成今日摘要
```

### 关键环境变量（详见 .env.example）

| 变量 | 说明 |
|---|---|
| `TRACE_MODE` | `test` / `offline` / `production`。production 下无 LLM Key / Telegram Token / 行情接入时**拒绝降级伪装**（显式 STOP 状态码） |
| `LLM_PROVIDER` | `gemini`（默认）或 `openai`（兼容 DeepSeek 等 OpenAI 兼容接口） |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | LLM 凭据（二选一，随 provider） |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_DEFAULT_CHAT_ID` | Bot 与默认接收人 |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | 美股行情（缺省时开发环境用 Mock，生产显式 unavailable） |

## 行为约定（重要）

- **生产门禁**：production 模式不得生成伪分析 / 伪投递 / Mock 行情，全部显式 STOP 或降级标记
  （`analysis_mode=rule_based_degraded`、`market_data_mode=mock|unavailable`）。
- **成本控制**：Level 1 确定性去重零成本拦截在 LLM 之前；真实 API 调用次数（含重试）
  记入 run 摘要的 `llm_stage_a_calls / llm_stage_b_calls / llm_verifier_calls`；
  Gemini 走 API 级 `responseSchema` 结构化约束，减少校验失败重试。
- **采集调度**：长驻 `run` / `bot` 循环按 `settings.yaml → collectors.interval_seconds`
  跳过未到期采集器并并行执行（`collectors.parallel_workers`）；`run-once` 验收不受影响。
- **运营闭环**（长驻循环每轮自动维护，全部幂等）：
  - 预测回测账本：事件 24h 后用真实行情核对方向预测，`/accuracy` 查看命中率；
  - 每日摘要：到 `digest.send_time`（用户时区）自动推送，每天一次；
  - 每日备份：SQLite 一致性快照到 `data/backups/`，保留 `backup.keep` 份；
  - 运行历史：每轮摘要落库，`/status` 查看最近运行 / 来源健康 / 回测 / 备份状态。
- **合规**：`license_mode=unknown` 的来源只做事实重述 + 链接，不重发正文；
  SEC 访问使用 `sec_contact` 配置的公平访问 User-Agent。

## 测试

```bash
pytest            # 默认排除 live 网络测试（-m "not live"）
```

## 部署

服务器（GCP）部署脚本与 systemd 单元见 `.deploy/`（`server_setup.sh` →
`install_service.sh`，服务名 `trace-run`）。验收证据与门禁报告见 `verification/`。
