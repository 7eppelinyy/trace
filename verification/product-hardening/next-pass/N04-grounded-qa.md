# N04 问答证据与模型调用专项验收报告

> 日期：2026-09-21 (Asia/Taipei)  
> 责任阶段：N04 - 完成问答证据与模型调用的专项验收 (P0/P1)  
> 运行环境：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11), Node v24.16.0

---

## 1. 任务范围与落地概况

根据 `docs/Trace_后续修正精确执行计划_2026-09-21.md` §7 (N04)，本阶段聚焦问答证据约束、模型费用熔断、多进程并发与拓扑传导的完整性收口：

1. **统一 LLM 调用预约与参数隔离 (`trace/ai/llm_client.py`)**：
   - 为 `complete_text` 增加 `usage_type="pipeline"` 与 `user_id` 支持，彻底杜绝自由文本调用绕过用户级/交互级 Ask 预算。
   - 为 `complete_json`、`complete_text` 增加输入提示词字符上限检查 (`100,000` 字符)，防止畸形载荷导致无界显存/内存溢出。
   - 修复结构化校验重试与网络重试的嵌套爆炸问题：在 `complete_json_validated` 内层网络重试上限收紧为 1，防止 `(retries + 1) * (retries + 1)` 的笛卡尔积调用放大，并增加多态 Mock 回退保护。

2. **多进程预算原子竞争与 UTC 跨日实测 (`tests/test_budget_concurrency.py`)**：
   - 编写两个真实独立子进程 (`subprocess.Popen`) 并发竞争同一 SQLite 文件数据库 (`IMMEDIATE` 事务) 的预算扣减测试。
   - 验证跨进程竞争下扣减总量严格守恒（不多扣、不少扣、恰好达到 `daily_limit`）。
   - 验证 UTC 跨日切换：前一日额度耗尽不影响次日额度，前一日调用计数历史在库中保持可查。
   - 验证 Ask 单用户限额 (`ask_user_limit`)、交互总限额与后台保留额度 (`pipeline_reserve`) 的严格隔离。

3. **事实硬约束与负例对抗测试 (`tests/test_grounded_answer_hardened.py`)**：
   - **事实 (fact)**：必须逐字完全匹配 Evidence 原文 (`text == quote` 且 `quote in source['text']`)；意译转述直接拦截并提示转为推断。
   - **推断 (inference)**：必须附带支持片段与显式成立条件 (`assumptions`)；数值推断必须能在支持片段中找到出处，严禁凭空捏造数字。
   - **情景 (scenario)**：必须显式标为 `kind='scenario'` 并附带明确假设。
   - **归因错误拦截**：引用的 quote 不在指定的 Evidence ID 原文中时直接抛出异常拒绝。

4. **语义局限性实证测试（结构合法不等于语义正确）**：
   - 专门编写测试用例并存档：说明模型即便通过了结构校验，仍可能存在无关引用、矛盾证据、期间/单位错配或跨实体混淆。
   - 验证系统渲染层诚实地将此类内容标记为【条件性推断（待验证）】、列出前置假设与支持片段，并生成待核验问题 (`next_checks`)，不伪造确定性。

5. **不可信材料与 Prompt 注入防御**：
   - 用户提问中的提示词泄露、DAN 越狱、角色覆盖被 `FinancialGuardrail` 确定性拦截（`jailbreak_blocked` / `out_of_domain`）。
   - 采集原文与历史上下文均作为 JSON 数据传入，被指令锁死为不可信材料；黑客在新闻中伪造的系统指令无法绕过事实硬约束。

6. **真实拓扑深度与模型版本追溯**：
   - 优化 `_build_graph_chain`：第 2 级标题忠实反映关系（直接为“一级直接敞口”，1-hop 为“产业链一级间接传导”，2-hop 为“产业链二级间接传导”，议题为“关联事件影响”），无影响时严格仅输出第 1 级事件记录。
   - 追溯字段：`AskAnswer` 与 `AskResponse` 增加 `model_version` 与 `prompt_version` 字段，离线或降级时诚实标为 `legacy_rule_based`。
   - 小程序前端 (`miniprogram/pages/ask/ask.js`) 补全 `invalid_model_output` 状态显示（“模型输出未通过事实核验”），纳入降级保护。

---

## 2. 自动化验证证据

### 2.1 专项测试结果

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q tests/test_grounded_answer_hardened.py tests/test_ask_grounding.py tests/test_llm_budget.py tests/test_budget_concurrency.py tests/test_llm_providers.py tests/test_ai_gates.py tests/test_hardening_boundaries.py
```
**输出**：
```text
82 passed, 1 warning in 9.84s
```

### 2.2 全量 Python 套件验证

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
**输出**：
```text
439 passed, 1 warning in 78.23s
```

### 2.3 小程序 Node 契约测试

```powershell
node miniprogram/tests/detail_mapping.test.js
node miniprogram/tests/session_research.test.js
```
**输出**：
```text
=== All Detail Mapping & Authenticity Tests Passed! ===
Session, token refresh, and research client contracts passed (device guest, token rotation, cache isolation, public capabilities).
```

---

## 3. 验收结论

| 条款 | 计划要求 | 验收状态 | 证据文件 / 用例 |
|---|---|---|---|
| **R04 / RV04** | 结构化问答与原文硬校验 | **已完成** | `tests/test_grounded_answer_hardened.py` (14 passed) |
| **R04** | 事实意译转述拒绝 | **已完成** | `test_fact_paraphrase_rejected` |
| **R04** | 数值幻觉拦截 | **已完成** | `test_inference_numerical_hallucination_rejected` |
| **R04** | 语义局限性实证 | **已完成** | `test_semantic_limitation_*` |
| **R04** | 两进程真并发竞争 | **已完成** | `test_budget_multiprocess_competition` |
| **R04** | UTC 跨日额度隔离 | **已完成** | `test_budget_utc_rollover` |
| **R04** | 用户预算与后台保留隔离 | **已完成** | `test_budget_ask_user_and_reserve` |
| **R04** | 动态拓扑层级忠实度 | **已完成** | `test_dynamic_graph_chain_hops`, `test_detail_mapping.test.js` |
| **R04** | 问答版本追溯 | **已完成** | `AskResponse.model_version`, `prompt_version` |
