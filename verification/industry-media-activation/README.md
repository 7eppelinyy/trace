# 产业媒体启用 Gate — 验收报告

- **日期**: 2026-08-31
- **结论**: `READY_INDUSTRY_MEDIA_ACTIVATION_V1`
- **净增量**: 启用源 11 → 13（TrendForce 因停更回退，实增 2）
- **触发**: 原 ph-1 只挂官方源，产业景气层（存储价格/供需）为 0 → 事件量掉到 1-2 条/天，告警自 8/28 起零推送。

---

## 1. 依据（从生产 VM 实测，非本地）

所有探测在 `trace-vm`（us-central1-a）上以 `.venv` + `TraceRadar/1.0` UA 完成，
产物在本仓库 `scripts/probe_media_feeds.py` → 运行结果见
`verification/industry-media-activation/media-feed-probe.json`。

| 来源 | feed 路径 | 结果 | robots | 判定 |
|---|---|---|---|---|
| TrendForce | `/news/rss`→`/news/feed_v2/` | 200 / 20 条 | 允许 | **停更 61 天**（最后条目 2026-07-01）→ 不启用 |
| DIGITIMES | `/rss/daily.xml` | 200 / 55 条（9 命中） | 允许 | ✅ 启用 |
| EE Times | `/feed/` | 200 / 10 条 | 允许 | ✅ 启用 |
| EE Times China | `/rss/news.xml` `/rss.xml` `/feed` | 全 404 | — | 不启用 |
| 集微网 | laoyaoba/jiweinet | 0 条 / 超时 | — | 不启用 |
| 芯智讯 | icsmart.cn | 断开 / 超时 | — | 不启用 |
| CNBC | search.cnbc.com/... | 200 / 30 条 | **Disallow** | 按 §6 不启用（能取≠获授权） |
| 财联社/东方财富 | /rss /feed | 404 / 0 条 | — | 不启用 |

## 2. 改动

| 文件 | 内容 |
|---|---|
| `trace/data/feeds.yaml` | DIGITIMES 换 `/rss/daily.xml`；新增 EE Times `/feed/`；移除失效的 EE Times China；两者挂 `keyword_filter=industry_media` + `bootstrap_days=7` |
| `trace/collectors/rss.py` | `handled_source_ids` 纳入 `src_digitimes`、`src_eetimes` |
| `trace/collectors/industry_media.py` | 移除 TrendForce/DIGITIMES 的 HTML 列表页路径（防双采集，且 HTML 无真实 pubDate/ETag） |
| `trace/data/seed_sources.yaml` | DIGITIMES/EE Times `enabled=true` + `license_mode=public`（官方 feed=明示授权）；TrendForce 保持 disabled 并注明停更；CNBC 注明 robots 禁止 |
| `trace/data/source_filters.yaml` | `industry_media` 词表扩充 30+ 条（产品/供需/价格信号词，中英） |
| `tests/test_industry_media_activation.py` | 新增 9 条回归（启停状态/采集器互斥/feed 一致性/词表匹配真实标题） |

## 3. 验证

- 本地 pytest：**153 → 162 passed**（9 条新增回归）
- 服务器 pytest：**162 passed**
- 真实 `run-once`（production）：
  - `raw_items_new=11, events_analyzed=11, alerts_sent=3`
  - DIGITIMES 拉 9 条（最新 pubDate 2026-08-31T09:15），EE Times 2 条
  - `keyword_filtered=129`（词表正确拦下无关条目，控住 LLM 成本）
  - TrendForce 20 条全被 bootstrap 窗口正确拦截（停更证据）
- 3 条真实 Telegram 推送已到私聊（`@traceyyy_bot`），其中 2 条来自新走通链路。

## 4. 已知限制

1. **TrendForce 无奈**：官方 feed 已于 2026-07-01 停更，其余路径 404。若要其新闻，
   需按 SanDisk 的 sitemap 路线重建（本 Gate 未做，避免无授权抓正文）。
2. 两条 `needs_human_review` 重复推送（Nvidia Q2 同标题两条，score 8.36 / 7.56）
   为既有 dedup 行为，非本 Gate 引入，未处理。
3. `# 合规约束不变`：DIGITIMES/EE Times 仅做 事实重述 + AI 摘要 + 来源名 + 原文链接，
   不重发全文。
