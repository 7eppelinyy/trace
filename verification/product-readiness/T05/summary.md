# T05 · 证据约束问答与真实分析图谱验证报告

## 1. 任务概述
- **任务编号**: T05 (P0/P1)
- **关联问题**: F05（回答与图谱结论强度超出模型证据、无证据虚构事实）、F08（缺少明确 model_ask 配置）、F25（SSE/交互治理）、F28（护栏文案夸大不可破甲承诺）
- **对应验收用例**: A13（无相关证据/答案空/伪造历史：证据不足或错误，不生成伪事实）
- **核心目标**:
  1. 事实证据模式 (`evidence_answer`) 下，无直接事实证据时严守真实性底线，返回 `insufficient_evidence` 与可操作的核验建议，杜绝凭空臆测行情与业务；
  2. 显式提供情景模拟模式 (`scenario`)，输出前缀显式警示情景假设与反证条件，不混入已核验事实流；
  3. 废除固定四级图谱生成器，按真实拓扑 hop 深度展开（0/1/2 条真实路径），绝不为了界面填满而凑齐四级；
  4. 支持事件锚定 (`event_id` / `event_version`)，结构化抽取与输出 `claims` 与 `citations`；
  5. 修正安全与拒答文案，移除不切实际的“不可破甲/绝对锁定”承诺。

---

## 2. 核心架构与代码变更

### 2.1 配置与参数扩展
- `trace/config.py`: `LLMConfig` 增加 `model_ask` 字段（默认回退至 `model` 或 `deepseek-chat`），并在 `load_config` 中正确解析。
- `trace/api/schemas.py`:
  - `AskRequest`: 增加 `event_id: str | None`, `event_version: int | None`, `mode: Literal["evidence_answer", "scenario"] = "evidence_answer"`, `history: list[ChatMessage]`。
  - `AskResponse`: 增加 `event_id`, `mode`, `claims: list[dict]`, `citations: list[str]`, `graph_chain: list[dict]`, `status`, `duration_ms`。

### 2.2 证据约束与问答引擎核心 (`trace/alerts/ask.py`)
- `AskEngine.ask` 方法重构：
  - **模式与证据门禁**:
    - `mode == "evidence_answer"`：本地无匹配事件证据时，立刻返回 `status="insufficient_evidence"`，提供核验建议与核验步骤，清空虚构图谱与 Claims；
    - `mode == "scenario"`：用户显式要求情景推演时，标记 `status="scenario_simulation"`，输出必须以 `【情景假设推演（非已发生事实）】` 强制前缀开头，Claims 标注为 `kind="scenario"`。
  - **事件锚定 (Event-Pinning)**:
    - 传入 `event_id` 与 `event_version` 时，直接从数据库按版本锚定，提取真实来源、影响分与原文链接。
  - **动态传导图谱 (`_build_graph_chain`)**:
    - 彻底废除原有硬编码固定 4 级模板；
    - 无证据且非情景推演模式下返回 `[]`；
    - 真实事件有直接影响但无拓扑邻居时，严格仅返回 2 级（L1: 事实来源, L2: 直接冲击）；
    - 仅当真实存在 1-hop 邻居标的时展开第 3 级；
    - 仅当真实存在多级拓扑节点时才展开第 4 级。
  - **Claims 与 Citations 结构化提取**:
    - 分类为 `fact`（已核验事实）与 `inference`（有条件推断，列出假设与反证指标）；
    - `citations` 汇聚真实原文 URL 或事件唯一凭证。

### 2.3 数据库原文链接检索与护栏修缮
- `trace/db/repositories.py` (`RawItemRepo.urls_by_events`):
  - 增强 SQL 查询联合 `raw_item.event_id` 与 `event_source.event_id`，确保通过官方证据关系均可精准追溯到原文 URL。
- `trace/ai/guardrails.py`:
  - 替换绝对化夸大用语（如“不可破甲铁律”、“绝对锁定”），改为严谨客观的安全拦截提示与买方合规研判规范。

### 2.4 小程序交互适配 (`miniprogram/pages/ask/ask.js` & `utils/api.js`)
- `utils/api.js`: `askQuestion` 支持透传 `mode`, `event_id`, `event_version`。
- `pages/ask/ask.js`: 明确区分 `scenario_simulation` 与 `insufficient_evidence` 视觉与标签展示，拒绝将证据缺失包装成事实已发生。

---

## 3. 测试验证矩阵

### 3.1 专用测试集 `tests/test_ask_grounding.py` (5/5 Passed)
| 用例编号 | 验证场景 | 预期行为 | 结果 |
| :--- | :--- | :--- | :--- |
| **ASK-01** | 无相关证据拒答 (`test_ask_no_evidence_refusal`) | 返回 `insufficient_evidence`，不编造业务/行情事实，给出核验指引 | **PASSED** |
| **ASK-02** | 情景假设模拟 (`test_ask_scenario_mode_with_assumptions`) | 显式声明假设前提，标记 `status=scenario_simulation`，Claims 均为 scenario | **PASSED** |
| **ASK-03** | 事件锚定与版本校验 (`test_ask_pinned_event_grounding`) | 准确提取指定事件的 Claims 与 citations；版本不匹配时拒答 | **PASSED** |
| **ASK-04** | 传导图谱真实拓扑深度 (`test_dynamic_graph_chain_hops`) | 孤立标的严格仅生成 2 级，不强凑 4 级模板，忠实于图谱拓扑 | **PASSED** |
| **ASK-05** | API 契约与多模式 (`test_api_ask_modes_and_grounding`) | 通过 FastAPI 客户端验证 `mode`、`claims`、`citations` 与状态契约 | **PASSED** |

### 3.2 护栏与安全测试集 `tests/test_guardrails.py` (6/6 Passed)
- 提示词泄露攻击拦截、角色劫持/DAN 拦截、系统标记伪造拦截、金融领域关键词放行及多轮对话上下文连续性测试全部通过。

### 3.3 整体 API 回归 (13/13 Passed)
- `tests/test_api.py` 全部通过，无任何破坏性回归。
