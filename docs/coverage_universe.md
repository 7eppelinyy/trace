# Trace 覆盖目录与数据源治理白皮书 (Coverage Universe & Source Governance)

> **版本**：1.0 (Gate G2 Production Readiness)  
> **发布日期**：2026-09-19 (Asia/Taipei)  
> **适用范围**：Trace 事件雷达、产业图谱引擎、自选服务与通知管线  
> **合规标准**：Zero Data Fabrication、License Scope Enforcement、Operational Override Isolation

---

## 1. 覆盖架构与三级分层模型

Trace 不是全市场、全行业无差别的新闻聚合器，而是**跨市场（美股/A股）半导体、存储与 AI 基础设施的证据与跟踪工作台**。为了保证证据的严肃性与推理的可溯源性，Trace 采用严格的三级覆盖分层机制：

```
┌────────────────────────────────────────────────────────────────────────┐
│ Tier 1: 核心实时监控池 (Real-time 24/7 Monitored Universe)             │
│ • 8 只核心标的：美股 3 只 (SNDK, MU, NVDA) + A 股 5 只 (中芯/兆易/澜起/江波龙/浪潮)│
│ • SLA：7×24 小时高频轮询 (120s~300s)，官方 IR / 监管披露直接绑定 security_map   │
│ • 触发规则：默认进入系统 Watchlist，触发实时推送与行情验证 (Market Confirmation) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Tier 2: 产业图谱上下文池 (Context Universe & Propagation Graph)        │
│ • 38 只产业中枢：涵盖晶圆代工、半导体设备、封测、材料、光模块、AI 服务器与 CSP │
│ • 美股 (17 只)：TSM, ASML, AMD, INTC, AVGO, QCOM, AMAT, LRCX, WDC, STX... │
│ • A 股 (21 只)：北方华创, 中微公司, 华虹, 海光, 寒武纪, 中际旭创, 工业富联... │
│ • 职能：承载产业链多跳传导分析，计算间接影响 (Indirect Impact) 与反证条件    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Tier 3: 用户自定义候选池 (Unverified Candidate Universe)               │
│ • 用户通过搜索或自选添加的代码格式合规标的                             │
│ • 状态生命周期：恒常标记为 status="unverified"，绝不自动升级为核验上市公司   │
│ • 隔离保障：用户设置的别名存入专属 user_alias，严禁污染公共上市公司主数据      │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 证券主数据目录 (Security Master Catalog - 46 只核心标的)

所有纳入主数据的标的均具备唯一 `security_id`（`SEC-{market}-{ticker}`），美股经过 SEC CIK 官方核验，A 股遵循沪深北交易所标准代码与后缀规范。

### 2.1 美股标的 (20 只)

| Ticker | 交易所 | CIK | 公司名称 (中/英) | 产业标签 | 覆盖层级 | 关联实体图谱节点 | 主要证据源 |
|---|---|---|---|---|---|---|---|
| **SNDK** | NASDAQ | 0002023554 | 闪迪 (Sandisk Corp) | memory, nand, storage | Tier 1 实时监控 | `sndk` | SEC EDGAR, SanDisk IR |
| **MU** | NASDAQ | 0000723125 | 美光科技 (Micron Technology) | memory, dram, nand, hbm | Tier 1 实时监控 | `mu` | SEC EDGAR, Micron IR |
| **NVDA** | NASDAQ | 0001045810 | 英伟达 (NVIDIA Corporation) | gpu, ai, datacenter | Tier 1 实时监控 | `nvda` | SEC EDGAR, NVIDIA IR |
| **TSM** | NYSE | 0001046179 | 台积电 (TSMC ADR) | foundry, advanced_node | Tier 2 图谱上下文 | `tsmc` | SEC EDGAR, 产业媒体 |
| **ASML** | NASDAQ | 0000937966 | 阿斯麦 (ASML Holding) | equipment, lithography, euv | Tier 2 图谱上下文 | `asml` | SEC EDGAR, 官方规则 |
| **AMD** | NASDAQ | 0000002488 | 超威半导体 (AMD) | cpu, gpu, ai | Tier 2 图谱上下文 | `amd` | SEC EDGAR, 产业媒体 |
| **INTC** | NASDAQ | 0000050863 | 英特尔 (Intel Corporation) | cpu, foundry | Tier 2 图谱上下文 | `intc` | SEC EDGAR, 产业媒体 |
| **AVGO** | NASDAQ | 0001730168 | 博通 (Broadcom Inc) | networking, asic, ai | Tier 2 图谱上下文 | `avgo` | SEC EDGAR, 产业媒体 |
| **QCOM** | NASDAQ | 0000804328 | 高通 (QUALCOMM Inc) | mobile, soc, 5g, ai | Tier 2 图谱上下文 | `qcom` | SEC EDGAR, 产业媒体 |
| **AMAT** | NASDAQ | 0000006951 | 应用材料 (Applied Materials) | equipment, deposition, etch | Tier 2 图谱上下文 | `amat` | SEC EDGAR, 商务部/BIS |
| **LRCX** | NASDAQ | 0000707549 | 泛林集团 (Lam Research) | equipment, etch, deposition | Tier 2 图谱上下文 | `lrcx` | SEC EDGAR, 商务部/BIS |
| **WDC** | NASDAQ | 0000106040 | 西部数据 (Western Digital) | storage, hdd, nand | Tier 2 图谱上下文 | `wdc` | SEC EDGAR, 产业媒体 |
| **STX** | NASDAQ | 0001137789 | 希捷科技 (Seagate Technology)| storage, hdd | Tier 2 图谱上下文 | `stx` | SEC EDGAR, 产业媒体 |
| **ARM** | NASDAQ | 0001973244 | 安谋 (Arm Holdings) | ip, cpu, architecture | Tier 2 图谱上下文 | `arm` | SEC EDGAR, 产业媒体 |
| **MRVL** | NASDAQ | 0001835632 | 迈威尔 (Marvell Technology) | networking, asic, storage | Tier 2 图谱上下文 | `mrvl` | SEC EDGAR, 产业媒体 |
| **AAPL** | NASDAQ | 0000320193 | 苹果 (Apple Inc) | consumer, nand_buyer | Tier 2 图谱上下文 | `apple` | SEC EDGAR, 供应链证据 |
| **MSFT** | NASDAQ | 0000789019 | 微软 (Microsoft Corp) | cloud, ai, hyperscaler | Tier 2 图谱上下文 | `msft` | SEC EDGAR, 官方公告 |
| **META** | NASDAQ | 0001326801 | Meta Platforms | cloud, ai, hyperscaler | Tier 2 图谱上下文 | `meta` | SEC EDGAR, 官方公告 |
| **GOOGL**| NASDAQ | 0001652044 | 谷歌 (Alphabet Inc) | cloud, ai, hyperscaler | Tier 2 图谱上下文 | `googl` | SEC EDGAR, 官方公告 |
| **AMZN** | NASDAQ | 0001018724 | 亚马逊 (Amazon.com) | cloud, ai, hyperscaler | Tier 2 图谱上下文 | `amzn` | SEC EDGAR, 官方公告 |

### 2.2 A股标的 (26 只)

| Ticker | 交易所 | 代码 | 公司名称 | 产业细分领域 | 覆盖层级 | 图谱节点 | 主要证据源 |
|---|---|---|---|---|---|---|---|
| **688981.SH** | 上交所 | 688981 | 中芯国际 | 晶圆代工 (Foundry) | Tier 1 实时监控 | `smic` | 巨潮资讯, 工信部 |
| **603986.SH** | 上交所 | 603986 | 兆易创新 | 存储芯片 (NOR/MCU/DRAM) | Tier 1 实时监控 | `gigadevice` | 巨潮资讯, 行业数据 |
| **688008.SH** | 上交所 | 688008 | 澜起科技 | 内存接口芯片 (DDR5/CXL) | Tier 1 实时监控 | `montage` | 巨潮资讯, JEDEC |
| **301308.SZ** | 深交所 | 301308 | 江波龙 | 存储模组与固件 (NAND Module) | Tier 1 实时监控 | `longsys` | 巨潮资讯, 现货行情 |
| **000977.SZ** | 深交所 | 000977 | 浪潮信息 | AI 服务器与算力集群 | Tier 1 实时监控 | `inspur` | 巨潮资讯, 供应链 |
| **688347.SH** | 上交所 | 688347 | 华虹公司 | 特色工艺晶圆代工 | Tier 2 图谱上下文 | `hua_hong` | 巨潮资讯 |
| **688041.SH** | 上交所 | 688041 | 海光信息 | 国产 CPU / DCU 算力 | Tier 2 图谱上下文 | `hygon` | 巨潮资讯 |
| **688256.SH** | 上交所 | 688256 | 寒武纪 | 云端 AI 训练/推理芯片 | Tier 2 图谱上下文 | `cambricon` | 巨潮资讯 |
| **688525.SH** | 上交所 | 688525 | 佰维存储 | 嵌入式存储与模组 | Tier 2 图谱上下文 | `biwin` | 巨潮资讯 |
| **300475.SZ** | 深交所 | 300475 | 香农芯创 | 存储芯片分销与产业联合 | Tier 2 图谱上下文 | `shannon` | 巨潮资讯 |
| **000021.SZ** | 深交所 | 000021 | 深科技 | 存储芯片封测 (OSAT) | Tier 2 图谱上下文 | `kaifa` | 巨潮资讯 |
| **600584.SH** | 上交所 | 600584 | 长电科技 | 先进封装与半导体封测 | Tier 2 图谱上下文 | `jcet` | 巨潮资讯 |
| **002156.SZ** | 深交所 | 002156 | 通富微电 | AMD/GPU 先进封测 | Tier 2 图谱上下文 | `tongfu` | 巨潮资讯 |
| **601138.SH** | 上交所 | 601138 | 工业富联 | AI 服务器代工与高速交换机 | Tier 2 图谱上下文 | `fii` | 巨潮资讯 |
| **603019.SH** | 上交所 | 603019 | 中科曙光 | 超算与高性能计算服务器 | Tier 2 图谱上下文 | `sugon` | 巨潮资讯 |
| **000938.SZ** | 深交所 | 000938 | 紫光股份 | ICT 基础设施与数据中心网络 | Tier 2 图谱上下文 | `unisplendour`| 巨潮资讯 |
| **300308.SZ** | 深交所 | 300308 | 中际旭创 | 800G/1.6T 高速光模块 | Tier 2 图谱上下文 | `innolight` | 巨潮资讯 |
| **300502.SZ** | 深交所 | 300502 | 新易盛 | 高速率光收发模块 | Tier 2 图谱上下文 | `eoptolink` | 巨潮资讯 |
| **300394.SZ** | 深交所 | 300394 | 天孚通信 | 光器件整体解决方案与光引擎 | Tier 2 图谱上下文 | `tfc` | 巨潮资讯 |
| **002371.SZ** | 深交所 | 002371 | 北方华创 | 刻蚀/薄膜沉积等半导体前道装备 | Tier 2 图谱上下文 | `naura` | 巨潮资讯, 工信部 |
| **688012.SH** | 上交所 | 688012 | 中微公司 | CCP/ICP 等离子体刻蚀设备 | Tier 2 图谱上下文 | `amec` | 巨潮资讯 |
| **688072.SH** | 上交所 | 688072 | 拓荆科技 | PECVD/ALD 薄膜沉积设备 | Tier 2 图谱上下文 | `piotech` | 巨潮资讯 |
| **688120.SH** | 上交所 | 688120 | 华海清科 | CMP 化学机械抛光设备 | Tier 2 图谱上下文 | `hwatsing` | 巨潮资讯 |
| **688019.SH** | 上交所 | 688019 | 安集科技 | CMP 抛光液与功能性湿电子化学品| Tier 2 图谱上下文 | `anji` | 巨潮资讯 |
| **002409.SZ** | 深交所 | 002409 | 雅克科技 | 前驱体材料与特种气体 | Tier 2 图谱上下文 | `yoke` | 巨潮资讯 |
| **603501.SH** | 上交所 | 603501 | 韦尔股份 | CIS 图像传感器与模拟芯片 | Tier 2 图谱上下文 | `will_semi` | 巨潮资讯 |

---

## 3. 数据源登记与合规授权治理 (Source Registry - 29 个源)

按照合规发布门禁标准（任务书 §7/§9/§11 与 F29），任何抓取来源必须登记四项明确许可权限：
- **`can_fetch`**：是否允许技术抓取与解析；
- **`can_store`**：是否允许在 SQLite/持久化存储中保存事实摘要与元数据；
- **`can_display`**：是否允许在 Web/小程序/Bot 界面中直接向用户展示；
- **`can_forward`**：是否允许推送到微信订阅消息或 Telegram 广播。

未获得授权的商业媒体（如路透社、彭博社等）一律标记 `enabled: false`，且四项许可均为 `false`，仅作为候选登记，严禁在生产中执行静默抓取。

### 3.1 官方监管与政策源 (15 个)

| Source ID | 来源名称 | 类型 | 权威等级 | 轮询周期 | 授权模式 | 许可范围 (F/S/D/W) | 启用状态 | 核验责任人 |
|---|---|---|---|---|---|---|---|---|
| `src_sec_edgar` | SEC EDGAR | official | official_regulator | 120s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_sndk_ir` | Sandisk IR | ir | official_company | 120s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_micron_ir` | Micron IR | ir | official_company | 120s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_nvidia_ir` | NVIDIA IR | ir | official_company | 120s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_fed` | 美联储 (Federal Reserve) | official | official_government | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_bis` | 美国商务部工业安全局 (BIS) | official | official_government | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_commerce` | 美国商务部 (GovDelivery) | official | official_government | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_federal_register` | 联邦公报 (Federal Register) | official | official_regulator | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_cninfo` | 巨潮资讯网 (沪深统一) | official | official_regulator | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_sse` | 上海证券交易所 | official | official_regulator | 300s | public | N / N / N / N | DISABLED (统一走巨潮) | Trace Architecture Team |
| `src_szse` | 深圳证券交易所 | official | official_regulator | 300s | public | N / N / N / N | DISABLED (统一走巨潮) | Trace Architecture Team |
| `src_miit` | 工业和信息化部 | official | official_government | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_mofcom` | 商务部 | official | official_government | 300s | public | Y / Y / Y / Y | **ENABLED** | Trace Compliance Team |
| `src_csrc` | 中国证监会 | official | official_regulator | 300s | public | N / N / N / N | DISABLED (选择器未决) | Trace Compliance Team |
| `src_stats_cn` | 国家统计局 | official | official_government | 600s | public | N / N / N / N | DISABLED (非实时) | Trace Compliance Team |

### 3.2 产业媒体源 (6 个)

| Source ID | 来源名称 | 类型 | 权威等级 | 轮询周期 | 授权模式 | 许可范围 (F/S/D/W) | 启用状态 | 核验说明 |
|---|---|---|---|---|---|---|---|---|
| `src_digitimes` | DIGITIMES | industry_media | industry_media | 600s | public | Y / Y / Y / Y | **ENABLED** | 每日公开 feed，仅做事实摘要 |
| `src_eetimes` | EE Times | industry_media | industry_media | 600s | public | Y / Y / Y / Y | **ENABLED** | 官方 feed，事实重述与引据 |
| `src_trendforce` | TrendForce | industry_media | industry_media | 600s | unknown | N / N / N / N | DISABLED | 官方 RSS 停更，仅候补登记 |
| `src_eetimes_china` | EE Times China | industry_media | industry_media | 600s | unknown | N / N / N / N | DISABLED | 公开 RSS 均已 404 |
| `src_jw_insights` | 集微网 | industry_media | industry_media | 600s | unknown | N / N / N / N | DISABLED | 未获正式商业数据转授权 |
| `src_chipwise` | 芯智讯 | industry_media | industry_media | 600s | unknown | N / N / N / N | DISABLED | 未获正式商业数据转授权 |

### 3.3 财经媒体与平台源 (8 个)

| Source ID | 来源名称 | 类型 | 权威等级 | 轮询周期 | 授权模式 | 许可范围 (F/S/D/W) | 启用状态 | 核验说明 |
|---|---|---|---|---|---|---|---|---|
| `src_jin10` | 金十数据 (MCP) | financial_media | financial_media | 600s | licensed_ai_analysis | Y / Y / Y / **N** | **ENABLED** | 官方 Bearer Token，仅内部分析展示，禁止转授权/转售 |
| `src_reuters` | 路透社 (Reuters) | financial_media | financial_media | 600s | licensed_ai_analysis | N / N / N / N | DISABLED | 无正式商业 API 授权前禁止接入 |
| `src_bloomberg` | 彭博社 (Bloomberg) | financial_media | financial_media | 600s | licensed_ai_analysis | N / N / N / N | DISABLED | 无正式商业 API 授权前禁止接入 |
| `src_dowjones` | 道琼斯 (Dow Jones) | financial_media | financial_media | 600s | licensed_ai_analysis | N / N / N / N | DISABLED | 无正式商业 API 授权前禁止接入 |
| `src_ft` | 英国金融时报 (FT) | financial_media | financial_media | 600s | licensed_ai_analysis | N / N / N / N | DISABLED | 无正式商业 API 授权前禁止接入 |
| `src_cnbc` | CNBC | financial_media | financial_media | 600s | unknown | N / N / N / N | DISABLED | robots.txt 明确禁止抓取 |
| `src_cls` | 财联社 | financial_media | financial_media | 600s | unknown | N / N / N / N | DISABLED | 未获商业授权前保持禁用 |
| `src_eastmoney` | 东方财富 / Choice | financial_media | financial_media | 600s | unknown | N / N / N / N | DISABLED | 未获商业授权前保持禁用 |

---

## 4. 运营状态隔离与持久化治理 (Operational Override - F30)

为了解决“**服务重启后 `load_all_seeds` 冲正运营人员手工禁用设置**”的核心工程缺陷，Trace 实现了严格的种子默认值与运营 Override 隔离：

1. **存储架构**：
   `source` 表包含 `enabled`（种子默认值）与 `operational_override`（运营人员手工覆盖值：`0` 强制停用，`1` 强制启用，`NULL` 遵循种子）。
2. **启动幂等写入防护**：
   在 `SourceRepo.upsert()` 执行 `ON CONFLICT(source_id) DO UPDATE SET` 时，更新权威等级、轮询间隔、安全映射等种子字段，但**严格排除** `operational_override`、`override_reason`、`override_updated_at`。
3. **有效性判断逻辑**：
   系统抓取器与健康探针仅通过 `SourceRepo.list_enabled()` 获取当前生效数据源：
   $$\text{EffectiveEnabled} = \text{COALESCE}(\text{operational\_override}, \text{enabled}) == 1$$
   这保证了运维人员在演练或合规突发事件中手工禁用的数据源，在容器崩溃重启、更新镜像或重新执行种子加载后，**始终保持安全禁用状态**。

---

## 5. 用户专属别名隔离保障 (User Watchlist Alias - F30)

1. **公共主数据独立性**：
   `security` 表为全局共享元数据，存储经过 SEC 或证券交易所官方认定的法定公司中英文名称（如 `NVIDIA Corporation` / `英伟达`）。
2. **私有别名存储**：
   用户在自选列表中的个人备注、自定义代称（如将 `NVDA` 命名为 `皮衣老黄旗舰` 或 `核心算力头寸`）统一持久化于 `watchlist.user_alias` 字段中。
3. **数据安全边界**：
   用户调用 `POST /api/v1/watchlist` 或 `PUT /api/v1/watchlist/{ticker}/alias` 时，后端仅修改自身用户的自选行，绝不修改共享 `Security` 实体，彻底杜绝多租户场景下公共主数据被恶意或随意篡改的问题。
