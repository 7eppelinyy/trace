# T08 · 以证据与变化为中心重做事件体验验证报告

## 1. 任务概述
- **任务编号**: T08 (P1)
- **关联问题**: F09（详情忽略 evidence URL、revision 列表、原始状态）、F14（列表 transmission_depth 机械式写 1 或 2，与图谱真实深度脱节）、F24（无请求序列保护、旧响应覆盖新响应、ask 无并发门禁）
- **核心目标**:
  1. 首页雷达与事件详情全面以“事实与变化”为中心，移除任何假 fallback（如 `apple-nand`、`14 家权威信源` 等虚构字段）；
  2. 详情页完整呈现证据工作台：事实与推断 Claims、原文链接（支持一键复制）、尚不清楚/待验证点、买方下一步跟踪锚点、真实传导图谱、版本修订时间线；
  3. 传导深度 (`transmission_depth`) 统一基于真实图谱路径与拓扑计算，直接对应实际产业链传导跳数，废除伪四级套话；
  4. 引入加载请求序号保护 (`loadGenerationCounter`)，防止慢网络下旧事件响应覆盖新点击事件；在问答端增加提交防重与并发门禁；
  5. 详情页点击追问时，精确透传 `event_id` 与 `event_version` 至问答页完成上下文锚定。

---

## 2. 核心架构与代码变更

### 2.1 API 模型与契约扩展 (`trace/api/schemas.py`)
- `EventDetailResponse` 扩展字段：
  - `transmission_depth: int`（基于真实拓扑与路径的传导跳数）
  - `claims: list[dict]`（结构化事实与推断，附带成立前提与反证指标）
  - `uncertainties: list[str]`（尚不清楚 / 待验证点）
  - `next_checks: list[str]`（买方下一步跟踪与核验重点）
  - `securities: list[EventSecurityChip]`（关联标的真实行情与研判方向）

### 2.2 事件路由与传导深度计算统一 (`trace/api/routers/events.py`)
- 在 `list_events` 与 `get_event_detail` 中统一传导深度口径：
  - 存在 2-hop 传导链时为 3；
  - 存在 1-hop 间接/条件影响时为 2；
  - 仅直接影响时为 1；无影响时为 0。
- 动态组装结构化 Claims、尚未披露的未知事实与下一步验证清单。

### 2.3 小程序详情与首页重构 (`miniprogram/pages/detail/*` & `pages/index/*`)
- `miniprogram/pages/detail/detail.js`:
  - 增加 `loadGenerationCounter` 请求序号保护，旧响应自动丢弃；
  - 增加 `copyEvidenceUrl` 复制原文链接能力；
  - `askAboutEvent` 完整透传 `pendingAskEventId` 与 `pendingAskEventVersion`。
- `miniprogram/pages/detail/detail.wxml`:
  - 完整布局：状态徽章与版本号 -> 核心事实与推断 Claims -> 权威信源与原文链接列表 -> 动态传导图谱 -> 尚不清楚点 -> 下一步验证 -> 版本修订时间线。
- `miniprogram/pages/detail/detail.wxss`:
  - 按照 Apple Design 现代极简风格布局，统一字阶、间距与语义配色。
- `miniprogram/pages/index/index.wxml`:
  - 移除对假案例 `apple-nand` 与 `14 家权威信源` 的 fallback，保证 100% 数据来自后端事实。
- `miniprogram/pages/ask/ask.js`:
  - 增加 `thinking` 并发防重门禁，并接收详情页传递的 `event_id` 与 `event_version` 绑定归因问答。

---

## 3. 测试验证矩阵

### 3.1 前端数据映射与真实性测试 `miniprogram/tests/detail_mapping.test.js` (7/7 Passed)
- A01: 无报价时不得伪造 1.5%（通过）
- A02: 缺失评分不得默认填入 7.0 分（通过）
- A03: reported 事件不得声称“官方源证实，已完成交叉校验”（通过）
- A04: 真实事件加载失败不得静默降级为 Apple NAND 案例（通过）
- A05: 未知 Demo ID 不得返回 Apple NAND（通过）
- A06: 直接影响事件严格仅生成 2 级传导，不强凑 4 级模板（通过）
- A07: 事实 Claims、原文证据、尚不清楚点与修订时间线正确映射（通过）

### 3.2 服务端 API 回归 `tests/test_api.py` (13/13 Passed)
- `test_events_list_and_detail`: 验证列表与详情的 `transmission_depth` 一致性（基于真实拓扑跳数）、`claims`、`uncertainties`、`next_checks`、`securities` 契约完整性。
