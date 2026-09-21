# N08 研究页面与真实用户动作闭环专项验收报告

> 日期：2026-09-21 (Asia/Taipei)  
> 责任阶段：N08 - 完成研究页面、偏好和共享的使用闭环 (P1)  
> 运行环境：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11), Node v24.16.0

---

## 1. 任务范围与落地概况

根据 `docs/Trace_后续修正精确执行计划_2026-09-21.md` §11 (N08)，本阶段围绕小程序与服务端投研闭环、并发冲突、正式分页、完整导出、公开分享安全及偏好写入口统一展开全链路建设：

1. **真实用户业务动作闭环 (`miniprogram/pages/research/research.js`, `trace/api/routers/research.py`)**：
   - 完成完整投研流转路径：从事件详情生成草案 -> 保存私有假设 -> 新到达 RawItem 自动匹配至待复核证据 -> 用户修订结论与私有笔记 -> 状态流转推进至已验证（`confirmed`）与归档（`archived`）。
   - 保证研究记录的私密性与租户隔离，未经验证的凭据或他人会话严禁读取。

2. **`match_new_evidence` 严格校验与来源策略过滤 (`trace/db/repositories.py`)**：
   - 关联新证据时严格核查 `raw_item` 存在性，不存在时抛出明确 `ValueError('Evidence not found')`。
   - 检查 `permitted(self.db, raw.source_id, 'display')`，若来源被限制展示（`can_display=False`），不予关联至假设，杜绝受限敏感数据渗漏。

3. **双请求竞争编辑与 409 保护 (`trace/api/routers/research.py`, `trace/db/repositories.py`)**：
   - 基于 `expected_revision` 乐观锁机制，当客户端携带旧版本修订号提交变更时，服务端返回 `409 Conflict`（`detail="Research has changed; reload before editing"`）。
   - 前端小程序保留用户已编辑表单输入内容，明确提示用户重新载入最新版本进行复核，杜绝静默覆盖或内容丢失。

4. **正式分页与真实计数支持 (`trace/api/routers/research.py`, `trace/api/schemas.py`, `miniprogram/pages/research/research.js`)**：
   - `ResearchQuestionRepo` 补齐 `count_by_user` 与 `offset` 查询参数。
   - `ResearchQuestionListResponse` 规范返回真实 `total`、`has_more`、`offset`、`limit`，彻底告别原先 `total=len(questions)` 造成的伪数据。
   - 小程序研究页接入 `onReachBottom` 自动触发 `loadMore`，支持大于 200 条海量研究记录平滑翻页，记录不再在界面失联。

5. **导出完整性与有界快照 (`trace/api/routers/research.py`)**：
   - `GET /research/export` 在事务内导出全量假设（`limit=None`）与全量反馈，支持 >1000 条记录导出，杜绝隐式静默截断。

6. **公开分享与私有笔记严格脱敏 (`trace/db/shares.py`, `trace/api/routers/research.py`)**：
   - 用户主动创建分享时生成限时不可伪造的 Capability Token。
   - 匿名访问公开快照时，严格剥离 `user_notes` 私密笔记。
   - 用户调用 `DELETE /research/questions/{id}/share` 撤销分享后，公开访问立即返回 404。

7. **草案条件明确标注待用户确认 (`trace/api/routers/research.py`)**：
   - 自动推导的推断依据与反证条件显式添加“【待用户确认的建议】”前缀，默认 7 天窗口明确标注为建议日期，不暗示新闻已验证财务数据或订单量级。

8. **提醒反馈单条可更新与范围权限 (`trace/api/routers/research.py`)**：
   - 基于 `sha256(user_id:event_id:event_version:security_id)` 生成稳定主键，用户重复点击同版本评价时原子更新，不膨胀评价分母。
   - 普通用户请求 `scope='my'` 只读自己；请求 `scope='all'` 时通过 `require_admin` 严格拦截并返回 403。

9. **Telegram `/alert` 与偏好仓储双写统一 (`trace/bot/telegram_bot.py`, `trace/db/repositories.py`)**：
   - 统一写入口：Telegram 机器人的 `/alert` 指令同时更新 `NotificationPreferenceRepo` 和 `AlertRuleRepo`；`NotificationPreferenceRepo.set_preference` 亦同步更新 `AlertRuleRepo`，彻底消除两张表阈值遮蔽或脱节的问题。

10. **全局总开关、免打扰与夏令时/时区合法性 (`trace/alerts/delivery_worker.py`, `trace/alerts/engine.py`)**：
    - `enabled=False` 时 Worker 发送前立即拦截全部提醒（返回 `notification_disabled`）。
    - 针对跨午夜时段（如 23:00 至 07:00）及夏令时时区换算进行了准确的区间判定验证。

---

## 2. 自动化验证证据

### 2.1 N08 专项测试套件

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -v tests/test_n08_research_and_user_actions.py
```
**实测输出**：
```text
============================= test session starts =============================
platform win32 -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
rootdir: D:\Trace
configfile: pytest.ini
plugins: anyio-4.15.1
collected 8 items

tests/test_n08_research_and_user_actions.py::test_full_research_workflow_lifecycle PASSED [ 12%]
tests/test_n08_research_and_user_actions.py::test_match_new_evidence_validation_and_source_policy PASSED [ 25%]
tests/test_n08_research_and_user_actions.py::test_concurrent_edit_returns_409_and_protects_latest_state PASSED [ 37%]
tests/test_n08_research_and_user_actions.py::test_pagination_and_true_total_count PASSED [ 50%]
tests/test_n08_research_and_user_actions.py::test_public_share_desensitization_and_revocation PASSED [ 62%]
tests/test_n08_research_and_user_actions.py::test_alert_feedback_single_opinion_and_scope_permissions PASSED [ 75%]
tests/test_n08_research_and_user_actions.py::test_telegram_cmd_alert_unification_with_preferences PASSED [ 87%]
tests/test_n08_research_and_user_actions.py::test_preferences_master_switch_and_quiet_hours PASSED [100%]

======================== 8 passed, 1 warning in 3.86s =========================
```

### 2.2 全量 Python 套件验证

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
**实测输出**：
```text
473 passed, 1 warning in 126.16s (0:02:06)
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

## 3. 用户动作闭环与边界验证表

| 功能模块 | 关键动作 | 异常与并发边界 | 服务端防护机制 | 前端/用户感知 | 状态 |
|---|---|---|---|---|---|
| **研究草案** | 事件详情生成草案 | 事件不存在 / 受限展示 | 严格返回 404，不泄露信息 | 提示无法读取事件 | **PASS** |
| **假设编辑** | 多端并发修改私有假设 | revision 预期不匹配 | 返回 409 Conflict | 提示重新载入，保留编辑输入 | **PASS** |
| **证据匹配** | 来源更正后自动关联 | 证据不存在 / 来源受限 | 校验 RawItem 存在与 display 权限 | 仅展示合法关联证据 | **PASS** |
| **列表浏览** | 查看海量研究 (>200) | 传统 LIMIT 截断 | limit + offset + true total | 触底加载更多，支持无限滚动 | **PASS** |
| **公开分享** | 生成限时快照供外部复核 | 个人私密敏感笔记外泄 | 快照严格剥离 user_notes | 接收者仅看公开研判要素 | **PASS** |
| **分享撤销** | 撤销已生成的公开快照 | 外部通过旧 token 继续偷看 | 撤销后实时返回 404 | 提示分享已失效 | **PASS** |
| **提醒反馈** | 对提醒评分与原因反馈 | 快速连击/重复打分膨胀 | 按用户/版本稳定哈希更新 | 维护单条有效意见 | **PASS** |
| **偏好设置** | Telegram /alert 与小程序设置 | 表结构不一致导致配置被覆盖 | 双向同步写两张表，保证一致生效 | 设定值即刻生效于投递管道 | **PASS** |
| **免打扰** | 夜间睡眠免打扰 | 跨午夜 (23:00~07:00) | 跨午夜区间准确判断 | 夜间静音保留至白天投递 | **PASS** |
