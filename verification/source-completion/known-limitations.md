# Known Limitations — Source Completion Gate V1

生成时间：2026-08-26（Asia/Taipei）

## 1. Micron 官方 RSS 端点不稳定

- `https://investors.micron.com/rss.xml` 在 2026-08-26 两次观测分别为
  **429（Cloudflare 限流）** 与 **404**。
- 处理：`MicronCollector` 低频（0.2 QPS）尝试 RSS，失败后回退官方新闻室页面
  `https://www.micron.com/about/newsroom/press-releases`（301 → `/about/press/news`），
  内容仍为官方一手，不使用第三方转载。
- 429 记录保留在 `source_health`（`last_http_status=429` 时派生状态 `RATE_LIMITED`），
  不被回退成功覆盖。
- 限制：newsroom 列表页只含最近约 12 条；更早公告不在回填范围（任务书 §16
  只要求最近 7–30 天，满足）。

## 2. SanDisk IR RSS 大陆直连超时

- 官方 IR RSS（`investor.sandisk.com` 域）在大陆网络直连超时。
- 处理：`SanDiskCollector` 改用官方主站 `https://www.sandisk.com/sitemap.xml`
  （直连可用），只取 `press-releases` 路径（当前 53 条），不依赖第三方。
- 限制：发布时间取自 URL 日期 slug + sitemap `lastmod`（无精确时分秒）。

## 3. U.S. Commerce 官网 403

- `commerce.gov` 主站对非浏览器请求返回 403（Cloudflare）。
- 处理：使用官方订阅分发渠道 **GovDelivery**
  （`https://public.govdelivery.com/accounts/USDOC/feed.rss`）作为真实入口。
- 限制：GovDelivery feed 更新频率低于官网，2026-08-26 观测最新条目为
  2026-03-19；半导体政策实时性主要由 BIS + Federal Register 覆盖，
  Commerce 作为权威链补位（同一事件走 same Event / different Evidence，不重复告警）。

## 4. 中国政府网站无结构化 feed

- 工信部 / 商务部无公开 RSS/JSON API，只能解析首页要闻列表。
- 处理：两级过滤——Level 1 中文关键词表（`trace/data/source_filters.yaml`）
  初筛后才进入 Event Engine；本轮实测关键词拦截 100 条无关条目。
- 限制：部分条目 `published_at` 只能解析到日期（无时分秒）；个别条目
  列表页不带日期则置空，不影响去重（canonical URL + title hash）。

## 5. TrendForce（P1）未启用

- 无 RSS（`/rss` 返回 404）；`robots.txt` 未禁止新闻页抓取，但站点条款
  未明确授权程序化抓取。
- 按任务书 §6：授权不明确 → `enabled=false`，仅作 `disabled_candidate`，
  不进入生产链路，不阻塞 Gate。

## 6. 语义去重依赖 Hash Embedder

- 未安装 `sentence-transformers`，运行时回退到 hash embedder；
  跨源合并（如媒体→官方确认）目前由引擎 `_merge_into` 在相似度命中后执行。
  纯靠标题措辞差异较大的跨源同事件可能暂不自动合并，
  由 official-confirmation revision 机制兜底（官方源到达即升级状态）。

## 7. 其他

- 新增政策源事件若与 watchlist 图谱无关联（`no graph hits`），不产生
  impact，不推送——这是设计行为，不是漏报。
- `src_csrc` / `src_stats_cn` 等 seed 中的预留源本轮保持 `enabled=0`，
  health 快照中显示的记录来自 doctor 探测（只读、不入库事件）。
