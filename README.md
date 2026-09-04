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
python -m trace.main review           # 人工检查队列（Schema 校验失败样本）
python -m trace.main review --resolve <REVIEW_ID>   # 标记已处理
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
  Gemini 走 API 级 `responseSchema` 结构化约束，减少校验失败重试；
  仅补充佐证的合并（事件无实质更新）默认不重跑 Stage B，省下的次数记入
  `stage_b_skipped`（开关 `ai.reanalyze_on_non_material_merge`）。
- **成本熔断**：`llm.daily_call_budget`（默认 3000，UTC 日切，0=不限制）在
  **每次真实 API 调用前**检查当日累计（含重试）。超限本轮返回
  `STOP_LLM_BUDGET_EXCEEDED` —— 停止而非静默降级成 `rule_based_degraded`。
  计数落库（`llm_usage` 表），重启不清零；`doctor` 与 `/status` 展示当日用量。
- **复推规则**：`alerts.revision_resend_rules` 是**白名单**——实质更新照常
  让 `event.version` +1（审计轨迹），但是否再次推送由白名单决定。
  `key_number_changed` 依赖持久化的 `event.key_numbers`；
  `direction_changed` / `score_delta_ge_1` 在 Stage B 之后比对前后结论得出。
- **开盘后补算**：盘后事件当时拿不到市场确认（见时段门禁），
  次日开盘由 `scoring.rescore_on_market_open` 重算 `final_score`（零 LLM 成本：
  `base_score` 已落库、供需信号是确定性函数）。此前低于阈值的事件可能因此
  跨过阈值并首次推送；已推送过的被幂等键拦下，不会重复打扰。
- **采集游标崩溃安全**：增量游标（`seen_accessions` / `etag` …）采用两阶段
  提交——`collect()` 期间只写内存，流水线走完 ingest 循环（RawItem 已落库）
  才推进。中途 STOP（预算耗尽 / 缺 Key / 进程重启）时游标保持原位，
  下一轮重新采集，重复条目由 Level 1 去重零成本拦掉。
  Schema 校验失败属于"走完了这一轮"，照常推进（否则每轮重烧一次 LLM）。
- **人工检查队列**：Schema 校验重试后仍失败的样本进 `human_review`，
  **不进入 Alert 链路**。用 `/review` 或 `python -m trace.main review` 查看
  （按失败类型聚合，是发现 prompt 退化的主要信号）。
- **市场确认的时段门禁**：`change_pct_from_prev()` 只是"当前价 vs 上一收盘"。
  事件之后市场还没开过盘（盘后/周末/节假日公告），或已超过
  `markets.confirmation_max_age_hours`，当前报价就与该事件无关 —— 市场确认
  一律取中性 5.0 并在 `market_data_mode` 标记原因，不把无关涨跌以 0.15 的
  权重掺进 `final_score`。参考时刻取「事件时刻（若当时开盘）或事件后首次开盘」，
  盘后事件不会因跨周末被误判为过期。
- **回测口径**：`/accuracy` 的命中率基于**事件锚定区间收益**
  （事件时刻快照价 → horizon 后价格），不是核对时刻的当日涨跌。
  拿不到事件锚点价、或服务停机导致核对严重超时的样本落账为 `unmeasurable`，
  不计入命中率、也不滞留在"待核对"。每条核对都保留
  `anchor_price / anchor_ts / exit_price / elapsed_hours` 供复算。
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
