# Trace MVP V1 真实端到端验收 — 最终报告

日期：2026-08-26（UTC+8）
最终状态：**WAIT_TRACE_MVP_V1_REAL_DATA_TELEGRAM_USER_ACCEPTANCE**

---

## 1. 验收结论

全部 15 项验收要求通过，系统在 production 模式下完成真实端到端链路：

真实 Collector → RawItem → Exact Dedup → Event/Revision → DeepSeek Stage A →
DeepSeek Stage B → Industry Graph → Scoring → Watchlist → Alert Engine →
Telegram → AlertDelivery Receipt

## 2. 环境诊断（doctor，15/15 OK）

- TRACE_MODE=production
- LLM_PROVIDER=openai（OpenAI 兼容实现，真实提供商为 DeepSeek）
- OPENAI_BASE_URL=https://api.deepseek.com，model=deepseek-chat
- TELEGRAM_BOT_TOKEN / TELEGRAM_DEFAULT_CHAT_ID 均已配置，Bot=@traceyyy_bot
- migration 已应用至 0004_bootstrap_suppression
- SEC EDGAR / NVIDIA IR / 巨潮 均 HTTP 200
- 行情：production 未接入 → market_data_mode=unavailable（禁止 Mock，已验证）

## 3. DeepSeek 真实调用验证

| 阶段 | 真实调用 | 结果 |
|---|---|---|
| Stage A 抽取 | ✅ analysis_mode=llm | 11 个必需字段全部输出（见 deepseek-stage-a.json） |
| Stage B 影响分析 | ✅ analysis_mode=llm | 12 个影响落库（见 deepseek-stage-b.json） |
| Stage B 负例（仅 filing 元数据） | ✅ | 全部 neutral/uncertain，conf=0.10，无违规 |
| Same-event Verifier | ✅ | same→True、different→False，双向正确 |
| Evidence Grounding 审计 | ✅ | 所有引用 ID 真实，无凭空方向结论 |

- 实际事件：`EVT-20260825-7018a4aefcda`「NVIDIA AI Factory Compute Is Becoming
  an Investable Asset Class」（NVIDIA 官方 IR，宣布与 6 家金融机构合作调动
  $500B 第三方资本支持 AI 基础设施）
- 负例：`EVT-20260825-3dc67608b433` SNDK 10-K（仅 filing 元数据 →
  全部方向为 neutral/uncertain，验证"不得仅凭提交 10-K 输出 bullish/bearish"）

## 4. 真实 Telegram 投递（acceptance replay）

- 命令：`python -m trace.main replay-event --event-id EVT-20260825-7018a4aefcda --acceptance-test`
- 顶部标记：🧪 历史真实事件验收回放（非实时新闻）
- 投递结果（真实回执，见 telegram-real-delivery-receipt.json）：

| 证券 | 方向 | 重要度 | 置信度 | message_id |
|---|---|---|---|---|
| NVDA 英伟达 | 🟢 偏利多（direct） | 8.6/10 | 90% | 3 |
| TSM 台积电 | 🟢 偏利多（nvda → tsmc） | 7.6/10 | 60% | 4 |

- 消息含：市场/代码/方向/重要度/置信度/事件/为什么重要/产业传导/
  事件状态（✅ 官方已确认）/来源（NVIDIA IR）/原文链接/用户本地时间/
  「⚠️ 行情确认：暂未接入」

## 5. 首次同步保护（bootstrap suppression）

- 用户 alert_activation_at=2026-08-26 05:09 UTC，事件时间=2026-08-11 16:38 UTC
- 不绕过检查时：0 条投递决策，2 条显式抑制记录（freshness_window）
- 历史事件保留在 Event DB 可查询，未作为即时 Alert 推送
- 未修改任何 published_at

## 6. 幂等验证

- replay 第二次运行：12 条影响全部被抑制，重复投递 = 0
- run-once 两次运行：无来源真实更新 → 86 条 RawItem 全部精确去重，
  events_created=0、alerts_sent=0、重复 Telegram Alert = 0
  （见 run-once-summary.json）

## 7. run-once 正式运行摘要（run-2bef4d5444）

```text
sources_checked: 8    sources_succeeded: 5    sources_failed: 0
sources_disabled: 3   raw_items_new: 0        raw_items_duplicate: 86
events_created: 0     events_revised: 0       events_analyzed: 0
alerts_eligible: 0    alerts_sent: 0          alerts_suppressed: 0
alerts_failed: 0      status: OK
```

## 8. 测试回归

`pytest tests -q`：**131 passed**（基线 115，未低于基线；
新增为首次同步保护/Gemini Provider 相关测试；修正 1 个 e2e 测试顺序
以符合首次同步保护的现实语义：用户须先于事件注册）

## 9. 证据清单（本目录）

- doctor-production.txt / deepseek-connectivity.json
- deepseek-stage-a.json / deepseek-stage-b.json /
  deepseek-stage-b-negative-filing-only.json
- deepseek-same-event-verifier.json / evidence-grounding-audit.json
- telegram-connectivity-receipt.json / telegram-user-watchlist.json
- real-event-input.json / rendered-alert.txt /
  telegram-real-delivery-receipt.json
- bootstrap-suppression-proof.json / production-no-mock-proof.json /
  duplicate-delivery-proof.json / run-once-summary.json
- pytest-output.txt
- 可复现脚本：run_llm_acceptance.py

所有证据文件均不含完整 API Key / Token。

## 10. 当前仍未完成的功能（不在本次验收范围）

1. 实时行情未真实接入（ALPACA_API_KEY 未配置）：market_data_mode=unavailable，
   消息显示「行情确认：暂未接入」，无任何 Mock
2. Micron IR / SanDisk IR 采集器因官方端点问题处于 disabled（3 个 disabled
   来源之一类）
3. 无自然发生的新重大事件通过本轮 run-once 投递（全部命中去重）：以历史
   真实事件验收回放完成真实投递验收，符合任务书 §9
4. Phase 2 / 小程序 / 更多新闻源 / 自动交易：均未开始（按要求停止）

## 11. 下一步

等待用户在 Telegram 手机客户端人工查看：
- 系统连通性测试消息（message_id=1/2 区间）
- NVDA 验收回放消息（message_id=3）
- TSM 验收回放消息（message_id=4）
