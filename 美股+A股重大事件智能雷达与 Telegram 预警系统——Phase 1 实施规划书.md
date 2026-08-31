# 美股 + A股重大事件智能雷达与 Telegram 预警系统
## Phase 1 MVP 工程实施规划书

### 1. 项目目标

开发一个面向**美股 + A股**的 7×24 小时重大事件智能监控系统。

系统不是普通财经新闻聚合器，也不是简单的“新闻 → LLM摘要 → Telegram”机器人。

核心目标是建立：

**多源信息 → Event事件 → 去重/聚类 → 公司与产业识别 → 美股/A股关联 → AI影响分析 → 可解释评分 → Telegram实时预警**

系统需要回答六个核心问题：

1. 发生了什么？
2. 这是不是一条真正的新事件？
3. 影响哪些美股/A股？
4. 为什么会影响？
5. 偏利多、利空、中性、混合还是不确定？
6. 这件事重要到需要立即提醒用户吗？

第一阶段**禁止自动交易、自动下单、自动使用杠杆**。

系统定位始终是：

> **市场事件发现与解释工具，而不是自动交易系统。**

---

# 2. Phase 1 范围

## 2.1 市场覆盖

必须从第一版架构开始同时支持：

- 美股
- A股

不得把系统写死为美股系统后再补 A股。

证券统一使用 `Security Master` 管理：

```text
security_id
market
exchange
ticker
company_name_zh
company_name_en
CIK
aliases
products
industry_tags
graph_node_ids
```

市场至少支持：

```text
US
CN
```

交易所至少预留：

```text
NASDAQ
NYSE
SSE
SZSE
BSE
```

---

# 3. 初始监控范围

## 美股核心 Watchlist

第一版重点保证：

```text
SNDK
MU
NVDA
```

Context Universe 包括但不限于：

```text
WDC
STX
AMD
AAPL
MSFT
META
GOOG/GOOGL
AMZN
TSM
AVGO
INTC
ARM
MRVL
AMAT
LRCX
```

以及非美股产业实体：

```text
Samsung
SK hynix
YMTC
CXMT
TSMC
```

Context Universe 不一定触发通知，但必须能够作为事件传播链中的节点。

## A股

A股不要求 Phase 1 一次性覆盖完整产业链。

第一版要求：

- 支持任意合法 A股代码加入 Watchlist；
- 建立第一批约 20–30 个 AI / 半导体 / 存储 / 服务器 / 光通信 / 半导体设备材料相关产业节点；
- 后续允许通过配置扩展。

不得把 A股产业映射硬编码在业务代码中。

---

# 4. 系统最重要的数据模型

系统中心对象必须是：

# Event

而不是：

# News

例如 Reuters、公司公告、SEC、CNBC、产业媒体都报道同一件事情：

不得生成五条独立提醒。

必须形成：

```text
Event
├── Evidence 1
├── Evidence 2
├── Evidence 3
├── Evidence 4
└── Evidence 5
```

系统同时保存：

```text
first_source
primary_source
all_sources
```

其中：

- `first_source`：最早发现事件的来源；
- `primary_source`：当前最权威证据；
- `all_sources`：所有证据。

例如：

```text
14:02 Reuters：据消息人士
14:21 CNBC：跟进
15:00 BIS：正式发布
```

Event 不变。

但：

```text
primary_source = BIS
event_status = official_confirmed
event_version += 1
```

如果属于重大更新，可以再次提醒用户，但必须显示：

```text
🔄 事件更新
```

不得伪装成新的事件。

---

# 5. Phase 1 数据源

## P0——必须优先实现

### 美股

- SEC EDGAR
- SNDK Investor Relations
- Micron Investor Relations
- NVIDIA Investor Relations
- Federal Reserve
- BIS
- Federal Register
- 美国重大半导体/贸易/出口政策官方源

### A股

- 巨潮资讯
- 上海证券交易所
- 深圳证券交易所
- 中国证监会
- 工信部
- 商务部
- 国家统计局
- 其他明确影响半导体/AI产业的官方政策源

## P1——产业信息

重点：

```text
TrendForce
DIGITIMES
EE Times
EE Times China
集微网
芯智讯
```

存储领域尤其关注：

```text
NAND
DRAM
HBM
SSD
Enterprise SSD
Memory Contract Price
Spot Price
Capacity
Utilization
AI Server
```

## P1/P2——财经媒体

预留 Provider Adapter：

```text
Reuters
Bloomberg
Dow Jones / Factiva
Financial Times
CNBC
财联社
东方财富 / Choice
```

但是：

**没有正式 API / License 时不得通过绕过付费墙、模拟登录或其他不稳定方式强行抓取。**

Reuters/Bloomberg/Dow Jones 等必须采用：

```text
Provider Interface
        ↓
Licensed Adapter
```

没有授权：

```text
enabled = false
```

不得让整个 MVP 因 Reuters/Bloomberg 尚未授权而无法运行。

---

# 6. 数据源合规原则

任何来源必须在 `source_registry` 保存：

```text
source_name
source_type
priority
base_reliability
license_mode
retention_policy
enabled
```

License Mode 至少支持：

```text
public
internal_only
licensed_ai_analysis
licensed_display
licensed_redistribution
unknown
```

`unknown` 默认采取最保守策略。

Telegram 默认只输出：

**事实重述 + AI生成中文摘要 + 来源名称 + 原文链接**

不得默认把 Reuters、FT、WSJ、DIGITIMES 等完整正文重新发布。

---

# 7. 数据采集架构

所有数据源必须实现统一 Collector Interface。

例如：

```text
Collector
├── SECCollector
├── IRCollector
├── CNINFOCollector
├── SSECollector
├── SZSECollector
├── PolicyCollector
├── RSSCollector
├── IndustryMediaCollector
├── ReutersCollector
└── MarketDataCollector
```

每条数据首先形成：

```text
RawItem
```

至少包含：

```text
source_id
source_item_id
title
url
published_at
fetched_at
language
content/reference
hash
```

---

# 8. 去重与 Event Engine

必须实现三级去重。

## Level 1：确定性去重

使用：

```text
source_item_id
canonical_url
normalized_title hash
normalized_content hash
```

## Level 2：近似重复

采用：

```text
title similarity
embedding similarity
entity overlap
```

## Level 3：跨语言 Event 聚类

例如：

```text
Nvidia faces new China chip restrictions
```

和：

```text
美国拟进一步限制英伟达AI芯片对华出口
```

应能够识别为同一个 Event。

第一版建议：

```text
merge_score =
0.35 * title_semantic_similarity
+ 0.25 * summary_semantic_similarity
+ 0.20 * entity_overlap
+ 0.10 * event_type_match
+ 0.10 * time_proximity
```

初始阈值：

```text
>= 0.82      自动合并

0.72–0.82    LLM same-event verifier

< 0.72       创建新 Event
```

阈值必须可配置，不允许散落硬编码。

---

# 9. Event Revision

不能错误去重真正的新进展。

例如：

```text
第一版：
美国政府“正在考虑”限制

第二版：
政策“正式公布”
```

应：

```text
same event_id
event_version += 1
material_update = true
```

以下情况允许重新推送：

- rumor → confirmed；
- 官方来源出现；
- 方向发生变化；
- 关键数字发生重大变化；
- final_score 变化 ≥ 1；
- 首次跨过用户提醒阈值。

---

# 10. AI 分析架构

不得：

```text
新闻全文 → LLM → 随便生成一段分析
```

必须拆成至少两个阶段。

## Stage A：Event Extractor

负责：

```text
发生了什么
涉及什么实体
关键数字
event_type
event_status
时间
事实
```

## Stage B：Impact Analyzer

负责：

```text
影响哪些证券
直接还是间接
产业传导路径
bullish / bearish / neutral / mixed / uncertain
directness
magnitude
persistence
reason
confidence
```

LLM 必须 Structured Output。

Schema 校验失败不得进入 Alert Engine。

---

# 11. 产业链关系

Phase 1 不建设复杂 Neo4j 知识图谱。

使用：

```text
SQLite industry_edge
+
Python adjacency graph
```

即可。

Edge 至少支持：

```text
supplier_of
customer_of
competes_with
substitutes_for
produces
consumes
depends_on
benefits_from
exposed_to
regulated_by
```

每条 Edge 保存：

```text
confidence
evidence_source
valid_from
valid_to
direction_rule
```

LLM 不得凭空永久修改产业图谱。

长期关系必须来自：

- 公司公告；
- SEC；
- IR；
- 官方资料；
- 高可信产业资料；
- 人工批准。

---

# 12. 最关键的差异化能力

系统不能只搜索股票代码。

必须支持：

```text
Event
↓
Industry Graph
↓
Security
```

例如：

```text
Microsoft AI CapEx ↑
↓
AI Data Center
↓
AI Server
↓
GPU / HBM / Enterprise SSD
↓
NVDA / MU / SNDK
```

即使原始新闻没有出现：

```text
SNDK
```

系统仍然可以发现潜在关联。

但必须明确区分：

```text
direct
indirect
conditional
```

不得把二跳、三跳影响包装成确定直接影响。

---

# 13. AI 不直接决定最终 1–10 分

禁止：

```text
“请LLM凭感觉给这条新闻打1–10分”
```

LLM 输出基础维度：

```text
directness
magnitude
persistence
```

来源可靠度由系统提供。

第一版：

```text
base_score =
0.22 * source_reliability
+ 0.28 * directness
+ 0.30 * magnitude
+ 0.20 * persistence
```

后续加入行情：

```text
final_score =
clamp(
    base_score
    + 0.15 * (market_confirmation - 5),
    1,
    10
)
```

评分公式必须：

- 集中管理；
- 可配置；
- 有测试；
- 可追溯。

---

# 14. Confidence 与 Importance 必须分离

例如：

```text
重要程度：9.2 / 10
置信度：58%
```

是合法状态。

含义：

> 如果事件属实影响非常大，但目前证据不足。

Telegram 应显示：

```text
⚠️ 高影响潜在事件
尚未获得官方确认
```

不得把 `confidence` 当作上涨/下跌概率。

---

# 15. Evidence Grounding

所有核心事实必须能关联：

```text
evidence.source_item_id
```

最终必须能够做到：

```text
结论
↓
Event
↓
Impact
↓
Industry Path
↓
Evidence
↓
Original URL
```

禁止模型虚构：

- 新闻；
- 来源；
- 公司公告；
- 产业关系；
- 数字。

无法证明的事实：

```text
删除
```

或者：

```text
needs_human_review = true
```

---

# 16. 行情数据

Phase 1 接行情，但行情暂时主要用于：

**Market Confirmation**

而不是主动无新闻异动报警。

美股 Provider Interface：

```text
USMarketDataProvider
```

首版优先：

```text
Alpaca
```

并允许未来替换：

```text
Massive
其他授权Provider
```

A股：

```text
CNMarketDataProvider
```

优先使用：

```text
Choice / 正式授权行情服务
```

开发阶段允许 Mock / 历史数据 / 合规开发数据。

不得让业务逻辑绑定某一个行情供应商。

至少提供：

```text
last price
previous close
1m
5m
15m
volume
volume ratio
pre/post market（美股）
```

---

# 17. 跨市场事件传播

必须考虑：

**美股和A股交易时间不同。**

例如：

```text
美国夜间 NVDA 重大事件
↓
A股休市
↓
次日上午 A股开盘
↓
观察国产GPU/服务器/光模块/半导体产业链反应
```

因此不能简单写：

```text
event + 30 minutes
```

必须支持：

```text
event_time
next_market_open
reaction_window
```

---

# 18. Telegram 第一版功能

至少实现：

```text
/watch SNDK
/watch 688xxx.SH

/remove SNDK

/watchlist

/alert SNDK 8

/alert all 7

/mute 60m

/unmute

/event EVENT_ID

/sources EVENT_ID

/digest

/timezone Asia/Tokyo

/ask SNDK 今天为什么跌

/help
```

A股证券格式应统一规范，不允许用户输入格式造成多个重复 Security。

---

# 19. Telegram 即时预警模板

最终用户看到的内容必须短、快、可解释。

示例：

```text
🚨 SNDK / MU 重大事件

影响：🔴 偏利空
重要度：8.6 / 10
置信度：87%

事件

Apple 据报道正在调整部分 NAND
采购策略，并评估扩大其他供应来源。

涉及证券

SNDK ↓  8.6
MU   ↓  6.8

为什么重要

若采购结构发生实质变化，可能影响现有
NAND供应商份额预期以及市场定价能力。

产业传导

Apple采购策略
→ NAND供应份额
→ NAND竞争与价格
→ SNDK / MU

市场确认

SNDK 15min：-2.4%
MU   15min：-1.1%

状态

⚠️ 媒体报道，尚未获得官方确认

来源

Reuters · 14:02

🕒 用户本地时间
```

底部使用按钮：

```text
[查看原文]
[其他来源]
[事件详情]
```

---

# 20. Watchlist 与通知规则

默认：

```text
final_score >= 7
```

才即时提醒。

允许：

```text
/alert SNDK 8
```

用户独立调整。

基础逻辑：

```text
if security in watchlist
and final_score >= threshold
and confidence >= minimum_confidence
and event_not_sent:
    send_alert()
```

重复提醒必须有 idempotency key：

```text
user_id
event_id
security_id
event_version
alert_type
```

机器人重启不能把历史消息重新发送。

---

# 21. 每日摘要

每日生成：

```text
📊 美股 + A股每日事件摘要
```

至少包括：

- 今日最高风险事件；
- Watchlist 每只股票涨跌；
- 今日主要事件；
- AI综合事件判断；
- 美股/A股跨市场关联；
- 下一交易日重点事件；
- 数据截止时间。

日报不得预测：

```text
“明天一定上涨”
“明天一定下跌”
```

重点回答：

```text
今天发生了什么？
风险在哪里？
明天需要观察什么？
```

---

# 22. /ask

Phase 1 支持：

```text
/ask SNDK 今天为什么跌？
```

但不得直接让模型自由联网回答。

必须从：

```text
Event DB
Evidence
Industry Graph
Market Data
```

中检索证据。

流程：

```text
Security
↓
Direct Events
↓
1-hop Industry Events
↓
2-hop Industry Events
↓
时间距离
↓
来源可靠度
↓
产业图距离
↓
同行共振
↓
候选原因排序
↓
生成解释
```

回答必须能够回到 Event ID 和 Evidence。

---

# 23. 数据库

Phase 1 使用：

```text
SQLite + WAL
```

暂不引入 PostgreSQL。

核心表至少：

```text
source
raw_item
event
event_source
event_revision

security
entity_alias
industry_edge
event_impact

market_snapshot

user
watchlist
alert_rule
alert_delivery

daily_digest
```

必须提供 migration 机制。

后续迁移 PostgreSQL 时不得要求重写核心 Domain Model。

---

# 24. 推荐项目结构

```text
project/
│
├── collectors/
│   ├── base.py
│   ├── sec.py
│   ├── ir.py
│   ├── rss.py
│   ├── cninfo.py
│   ├── sse.py
│   ├── szse.py
│   ├── policy.py
│   ├── industry_media.py
│   └── market_data/
│
├── event_engine/
│   ├── normalize.py
│   ├── exact_dedup.py
│   ├── semantic_cluster.py
│   └── revision.py
│
