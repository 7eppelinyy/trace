# N07 来源治理与发送、展示旁路专项验收报告

> 日期：2026-09-21 (Asia/Taipei)  
> 责任阶段：N07 - 核查来源治理与发送、展示旁路 (P0/P1)  
> 运行环境：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11), Node v24.16.0  
> 数据库迁移：`0031_source_governance_authorization.sql`

---

## 1. 任务范围与落地概况

根据 `docs/Trace_后续修正精确执行计划_2026-09-21.md` §10 (N07)，本阶段针对四大能力位（`fetch`、`store`、`display`、`forward`）在系统各层级的执行及所有旁路漏洞进行了全面核查与严格闭环：

1. **授权表迁移与数据模型隔离 (`trace/db/migrations/0031_source_governance_authorization.sql`, `trace/domain/models.py`)**：
   - 创建 `source_authorization` 审计表，包含 `auth_id`, `source_id`, `scope_json`, `evidence_url_or_file`, `verified_by`, `verified_at`, `expires_at`, `status`, `notes`, `created_at`。
   - 严格区分“系统技术配置权限（can_fetch/store/display/forward）”与“第三方数据真实法律/合规许可记录（SourceAuthorizationRecord）”。
   - 保持 seed 来源文件中的 `verified_at` 和 `verified_by` 为 `null`，不再出现虚构的合规团队或预填通过审批时间。

2. **采集层 `can_fetch` 治理与探针维护旁路阻断 (`trace/collectors/base.py`)**：
   - 调度层 `enabled_sources` 排除 `can_fetch=False` 的数据源，采集器跳过未授权抓取。
   - `BaseCollector.http_client(source_id)` 显式执行 `permitted(self.source_repo.db, source_id, "fetch")`。若未获授权直接抛出 `PermissionError`，彻底杜绝任何后台 maintenance、手动 probe 或 ad-hoc 脚本发起的非法抓取旁路。

3. **流水线 `can_store` 严格过滤与游标推进 (`trace/pipeline.py`)**：
   - 流水线在条目摄入层（Stage A 之前）逐条校验 `permitted(ctx.db, item.source_id, 'store')`。
   - 未被允许存储的来源条目被阻断持久化，记录 `source_storage_not_permitted:{source_id}` 审计日志。
   - 增量游标正常推进提交（`commit_cursors()`），杜绝每轮循环由于不可存储条目未推进游标而重复调用大模型消耗预算。

4. **展示层 `can_display` 贯穿事件、Ask 检索、研究草案与公开分享 (`trace/api/routers/`, `trace/alerts/ask.py`)**：
   - `/events` 列表和 `/events/{id}` 接口：受限来源事件完全隐藏，详情返回 404，不泄露衍生信息。
   - `/ask` 归因问答：语义候选排序与事件显式锚定（`event_id`）均严格过滤 `event_permitted(..., 'display')`；受限事件直接返回 `insufficient_evidence`。
   - `/research/questions/draft`：草案生成校验事件展示权限，受限时返回 404。
   - `/research/questions`：创建假设跟踪时校验关联事件权限，受限时返回 403。
   - `/research/questions/{id}/evidence`：关联新证据时校验 `permitted(ctx.db, raw.source_id, "display")`，受限时返回 403。
   - `/research/questions/{id}/share`：创建公开分享时校验事件权限（403 阻断）；访问已创建的公开快照时，若来源在入队后被吊销（`can_display=False`），快照访问实时返回 404，杜绝通过历史快照绕过策略撤回。
   - **多来源事件保守原则**：对于多源汇聚事件，若关联的任何一个原始条目来源不允许展示，整体事件保守视为不可展示。

5. **投递层 `can_forward` 阻断 Worker、CLI 回放、Bot 与每日摘要 (`trace/alerts/delivery_worker.py`, `trace/main.py`, `trace/bot/telegram_bot.py`, `trace/alerts/digest.py`)**：
   - **Outbox Worker 投递前复查**：即使任务在策略撤销前已进入 Outbox，Worker 发送前调用 `delivery_policy` 检查，立即置为 `suppressed` 并记录 `source_forward_not_permitted`。
   - **每日摘要**：`DigestBuilder.build` 生成时自动过滤 `can_forward=False` 或 `can_display=False` 的事件。
   - **CLI 回放**：`cmd_replay_event` 在执行 Stage B / 推演前校验 `event_permitted(app.db, event_id, "forward")`，若受限立即 `SystemExit` 阻断。
   - **Telegram Bot 指令**：`/event` 与 `/sources` 指令双重校验 `display` 与 `forward`，受限时明确回复拒绝展示或转发。

6. **配置重启与 Seed 更新的非破坏性机制 (`trace/db/repositories.py`)**：
   - `SourceRepo.upsert` 采用 `COALESCE(source.verified_at, excluded.verified_at)` 与 `COALESCE(source.verified_by, excluded.verified_by)`，应用重启加载 seed 配置时不会覆盖合规人员登记的真实授权时间与审批人。
   - `enabled` 字段受 `operational_override` 保护，重启时不会冲掉运营人员的手工干预（`set_operational_override`）。

---

## 2. 自动化验证证据

### 2.1 N07 专项测试套件

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -v tests/test_n07_source_policy_and_bypasses.py
```
**实测输出**：
```text
============================= test session starts =============================
platform win32 -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: D:\Trace
configfile: pytest.ini
plugins: anyio-4.15.1
collected 13 items

tests/test_n07_source_policy_and_bypasses.py::test_can_fetch_disabled_in_collector_and_blocks_http_client PASSED [  7%]
tests/test_n07_source_policy_and_bypasses.py::test_can_store_false_skips_storage_commits_cursor_and_avoids_llm PASSED [ 15%]
tests/test_n07_source_policy_and_bypasses.py::test_can_display_false_hides_from_api_events_and_detail PASSED [ 23%]
tests/test_n07_source_policy_and_bypasses.py::test_can_display_false_excludes_from_ask_engine PASSED [ 30%]
tests/test_n07_source_policy_and_bypasses.py::test_can_display_false_blocks_research_draft_create_and_evidence PASSED [ 38%]
tests/test_n07_source_policy_and_bypasses.py::test_public_share_blocked_at_creation_and_revoked_on_policy_change PASSED [ 46%]
tests/test_n07_source_policy_and_bypasses.py::test_multi_source_conservative_withholding_rule PASSED [ 53%]
tests/test_n07_source_policy_and_bypasses.py::test_can_forward_false_suppresses_outbox_delivery PASSED [ 61%]
tests/test_n07_source_policy_and_bypasses.py::test_can_forward_false_excluded_from_daily_digest PASSED [ 69%]
tests/test_n07_source_policy_and_bypasses.py::test_cli_replay_event_halts_when_forwarding_not_permitted PASSED [ 76%]
tests/test_n07_source_policy_and_bypasses.py::test_telegram_bot_commands_reject_unpermitted_events PASSED [ 84%]
tests/test_n07_source_policy_and_bypasses.py::test_source_upsert_preserves_verified_fields_and_operational_override PASSED [ 92%]
tests/test_n07_source_policy_and_bypasses.py::test_source_authorization_record_lifecycle PASSED [100%]

======================== 13 passed, 1 warning in 4.59s ========================
```

### 2.2 全量 Python 套件验证

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
**实测输出**：
```text
465 passed, 1 warning in 124.75s (0:02:04)
```

### 2.3 小程序 Node 契约测试

```powershell
node miniprogram/tests/detail_mapping.test.js
node miniprogram/tests/session_research.test.js
```
**实测输出**：
```text
=== Running T01 Detail Mapping & Authenticity Tests ===
✔ A01 Passed: No fake change_pct generated for bullish impact without quote
✔ A02 Passed: Missing score does not default to 7.0
✔ A03 Passed: Unconfirmed event does not claim official verification or cross check
✔ A04 Passed: Failed real event does not fallback to Apple NAND mock
✔ A05 Passed: getMockDetailById does not silently fallback to Apple NAND
✔ A06 Passed: Direct-only event strictly outputs 2-level topology chain
✔ A07 Passed: Rich evidence, claims, and uncertainties correctly mapped
=== All Detail Mapping & Authenticity Tests Passed! ===

Session, token refresh, and research client contracts passed (device guest, token rotation, cache isolation, public capabilities).
```

---

## 3. 四能力位覆盖矩阵与旁路清查清单

| 能力位 | 核心责任 | 保护入口 | 曾存旁路风险 | 处置与验证方式 | 状态 |
|---|---|---|---|---|---|
| **fetch** | 来源请求许可 | 调度器 `enabled_sources` | 维护探针或直接调 `http_client` 发起请求 | `BaseCollector.http_client` 强制核查 `permitted(..., 'fetch')`，未授权抛出 `PermissionError` | **PASS** |
| **store** | 原始数据存储许可 | `Pipeline.run_once` | 存储失败后游标停滞导致无限重复调用模型 | 阻断入库并推进游标；记录 `source_storage_not_permitted` | **PASS** |
| **display** | 界面与分析展示许可 | `/events`, `/ask`, `/research` | 衍生推演泄露、公开快照过期后仍可读、多源信息泄露 | 事件详情 404、问答排除、草案 404、快照失效校验、多源保守 withholding 规则生效 | **PASS** |
| **forward** | 外发与通知广播许可 | Outbox Worker, CLI, Bot, Digest | 入队后撤回继续发送、CLI 回放绕过、Bot 独立指令泄露 | Worker 投递前复查、CLI 回放阻断、Bot 指令拦截、日报过滤 | **PASS** |

---

## 4. 外部条件与合规说明

> [!IMPORTANT]
> **真实商务与法律授权声明：**
> 本地测试及自动化套件中的四能力位校验已全部通过。种子数据配置中的 `verified_at` 和 `verified_by` 维持真实未审批状态（`null`），不制造虚假合规记录。后续商用上线前，需由合规负责人签署真实数据授权协议并调用 `SourceRepo.record_authorization` 录入。该门禁作为外部前置条件，记录在 N12 外部条件登记表中。
