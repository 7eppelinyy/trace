# Final Report — Trace Source Completion Gate V1

生成时间：2026-08-26 18:20（Asia/Taipei）

---

## 1. 最终状态

```
READY_TRACE_SOURCE_COMPLETION_GATE_V1
```

G1–G13 全部 PASS（见文末 Gate 清单）。停止于此，等待下一步
`Trace → Google Cloud 7×24 Deployment`。

## 2. 新增真实来源（本轮 enabled=1，真实抓取验证通过）

| source_id | 名称 | authority | 入口 |
|---|---|---|---|
| `src_sndk_ir` | SanDisk IR | official_company | 官方主站 sitemap（直连可用） |
| `src_micron_ir` | Micron IR | official_company | 官方新闻室页面（RSS 429 回退） |
| `src_commerce` | U.S. Department of Commerce | official_government | GovDelivery 官方订阅 RSS |
| `src_miit` | 工业和信息化部 | official_government | 首页要闻 + 中文关键词初筛 |
| `src_mofcom` | 商务部 | official_government | 首页要闻 + 中文关键词初筛 |

## 3. Disabled 来源（配置预留，不进生产链路）

`src_trendforce`（P1 候选，授权不明）、`src_reuters`、`src_bloomberg`、
`src_cnbc`、`src_ft`、`src_dowjones`、`src_cls`（财联社）、
`src_eastmoney`（东方财富）、`src_digitimes`、`src_eetimes`、
`src_eetimes_china`、`src_jw_insights`（集微网）、`src_chipwise`（芯智讯）、
`src_csrc`、`src_stats_cn`、`src_sse`、`src_szse` —— 全部 `enabled=0`。

## 4. SanDisk IR 使用的真实入口

`https://www.sandisk.com/sitemap.xml`（官方主站，200，53 条 press-releases）。
原 `investor.sandisk.com` IR RSS 大陆直连超时，已弃用旧失效 URL。
解析：标题 / URL 日期 slug 发布时间 / canonical URL / 唯一 ID / 摘要，
ETag + Last-Modified 条件请求，30 天回填窗口，未使用任何第三方转载。

## 5. Micron IR 使用的真实入口

`https://www.micron.com/about/newsroom/press-releases`
（301 → `/about/press/news`，200，官方新闻室列表页）。
解析官方新闻稿链接（`investors.micron.com/news/press-release/...`）、
"Month D, YYYY" 发布时间、标题。

## 6. Micron 429 是否解决

**是（回退路线）**。官方 RSS 实测 429/404 交替；不提高频率硬闯，
改为低频（0.2 QPS）尝试 RSS → 失败回退官方新闻室页面。
429 在 `source_health` 显式记录（`last_http_status=429` →
派生状态 `RATE_LIMITED`），不显示为"无新数据"。本轮 Micron 采集
成功拿到 3 条新 RawItem（Training Center / Research Labs / Ventures Fund）。

## 7. 美国政策来源

已有 `src_bis` + `src_federal_register` + `src_fed` 之外，本轮新增
`src_commerce`（GovDelivery RSS）。Commerce/BIS/Federal Register
同一事件走 same Event + different Evidence（authority chain 保留），
跨源去重证据见 `cross-source-dedup-proof.json`；官方确认修订证据见
`official-confirmation-revision-proof.json`。

## 8. 中国政策来源

`src_miit`（工信部）+ `src_mofcom`（商务部），均 `enabled=1`。
两级过滤：Level 1 中文关键词表（半导体/集成电路/存储/出口管制等，
集中在 `trace/data/source_filters.yaml`）→ 候选才进 DeepSeek。
本轮实测关键词拦截 100 条无关条目。

## 9. 是否接入 TrendForce

**否**。无 RSS（`/rss` → 404），robots.txt 未禁新闻页但站点条款未明确
授权程序化抓取。按任务书 §6 授权不明确 → `enabled=false`，
仅 `disabled_candidate`，不作硬门。

## 10. 每个来源最近一次真实抓取时间（UTC，2026-08-26 两轮真实 run）

| 来源 | last_success_at | 状态 |
|---|---|---|
| src_sec_edgar | 10:04:08 | HEALTHY |
| src_nvidia_ir | 10:04:09 | HEALTHY |
| src_sndk_ir | 10:04:10 | HEALTHY |
| src_micron_ir | 10:04:16（复跑 10:15:46） | HEALTHY*（RSS 429 记录保留） |
| src_bis | 10:04:21 | HEALTHY |
| src_miit | 10:04:21 | HEALTHY |
| src_mofcom | 10:04:21 | HEALTHY |
| src_cninfo | 10:04:19 | HEALTHY |
| src_federal_register | 10:04:23（复跑 304） | HEALTHY |
| src_fed | 10:04:27（复跑 304） | HEALTHY |
| src_commerce | 10:04:27（复跑 304） | HEALTHY |

*Micron：newsroom 回退成功，`consecutive_failures=0`；429 历史显式可查。

## 11. 新增 RawItem 数

首轮接入（run-b2e72cd6ca）：75 collected → **20 new**（55 精确去重命中）。
按源：sndk 8、micron 3、miit 4、mofcom 2、bis 2、federal_register 1。

## 12. 去重后 Event 数

**20 个新 Event**（events_created=20，events_revised=0）。
历史库总 Event 233（含既有 213）。无重复 Alert。

## 13. LLM 调用数量

首轮：Stage A = 20，Stage B = 13，verifier = 0。
幂等复跑（run-009fe8543a）：13 collected → 全部精确去重命中，
**Stage A/B/verifier = 0**。关键词初筛拦截 100 条。
新增 5 个来源未导致 LLM 调用线性暴增（任务书 §17 达标）。

## 14. Telegram 是否产生额外噪声

**无**。两轮真实 run：`alerts_sent=0`，`alerts_suppressed=211`
（bootstrap suppression：`before_activation` + `freshness_window`）。
既有 2 条历史真实投递不受影响。无历史公告集中推送。

## 15. Source Health

11 个启用来源全部 `consecutive_failures=0`；Micron 429 显式记录
（`last_http_status=429` 时派生 `RATE_LIMITED`，测试覆盖）；
状态枚举：HEALTHY / DEGRADED / RATE_LIMITED / BROKEN / DISABLED，
`doctor` 命令输出完整快照。详见 `source-health.json`。

## 16. 最终测试数量

**153 passed**（baseline 131 → 153，新增 22 个源完成测试，无回归）。
`verification/source-completion/pytest-output.txt`。

## 17. 已知限制

见 `known-limitations.md`：Micron RSS 端点不稳定（回退生效）、
SanDisk IR RSS 直连超时（sitemap 替代）、Commerce 官网 403
（GovDelivery 替代）、中国政府网站无结构化 feed（关键词初筛兜底）、
TrendForce 授权不明（disabled_candidate）、Hash Embedder 语义去重局限。

## 18. 证据路径

`verification/source-completion/`：

```
baseline.txt                                # 基线快照
source-registry.json                        # 全量来源注册表
live-source-smoke.json                      # 逐源真实网络 smoke test
sandisk-ir-sample.json                      # SanDisk 真实样本（Collector→RawItem→Event→Evidence）
micron-ir-sample.json                       # Micron 真实样本
us-policy-sample.json                       # 美国政策源样本
cn-policy-sample.json                       # 中国政策源样本
source-health.json                          # 统一健康检查快照
bootstrap-suppression-proof.json            # 首次接入历史事件全部抑制
cross-source-dedup-proof.json               # 跨源 same-event 去重
official-confirmation-revision-proof.json   # 官方确认修订（version+1）
llm-call-filter-stats.json                  # DeepSeek 成本控制统计
pytest-output.txt                           # 153 passed
known-limitations.md
final-report.md                             # 本文件
run-once-output.txt                         # 首轮接入真实 run
run-once-repeat-output.txt                  # 幂等复跑（新增=0，LLM=0）
```

---

## Gate 清单（任务书 §19）

| Gate | 检查项 | 结果 |
|---|---|---|
| G1 | SEC real | PASS（200，CIK submissions 实测） |
| G2 | NVIDIA IR real | PASS（200，rss.xml 20 条） |
| G3 | SanDisk IR real | PASS（sitemap 200，53 条，新样本入库） |
| G4 | Micron IR real | PASS（newsroom 回退 200，3 条新样本；429 显式记录） |
| G5 | CNINFO real | PASS（hisAnnouncement 200） |
| G6 | BIS / Federal Register | PASS（均 200，新样本入库） |
| G7 | U.S. Commerce policy source | PASS（GovDelivery 200，bootstrap 入库） |
| G8 | China policy source >= 2 | PASS（工信部 + 商务部，均启用并出新样本） |
| G9 | source health | PASS（11 源 0 连续失败，状态枚举完整） |
| G10 | bootstrap suppression | PASS（211 抑制 / 0 新告警 / 历史事件不推送） |
| G11 | duplicate-event control | PASS（Event→N Evidence + 官方确认 revision + 幂等复跑 0 新增） |
| G12 | LLM cost filter | PASS（关键词拦截 100，复跑 0 LLM 调用） |
| G13 | 131+ tests | PASS（153 passed） |

TrendForce 不作硬门：`disabled_candidate`。
