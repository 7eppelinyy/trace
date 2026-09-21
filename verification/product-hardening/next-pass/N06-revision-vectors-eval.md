# N06 事件修订、向量版本与独立评测专项验收报告

> 日期：2026-09-21 (Asia/Taipei)  
> 责任阶段：N06 - 收口事件修订、向量版本和独立评测 (P1)  
> 运行环境：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11), Node v24.16.0

---

## 1. 任务范围与落地概况

根据 `docs/Trace_后续修正精确执行计划_2026-09-21.md` §9 (N06)，本阶段围绕文档修订链路、多版本保留、状态机流转、向量失效与模型身份跟踪、以及 Label-blind 评测隔离展开全方位收口：

1. **同文档 ID 修订与原始条目完整保留 (`trace/event_engine/exact_dedup.py`, `trace/event_engine/engine.py`, `trace/db/repositories.py`)**：
   - 基于迁移 0025 `UNIQUE(source_id, source_item_id, content_hash, title_hash)` 约束，当相同 `source_item_id` 发布更正内容时，系统保留原 `RawItem`，并完整插入新的 `RawItem`。
   - 两份 `RawItem` 均通过 `link_event` 关联至同一个 `event_id`；事件主版本自增（$v=1 \to v=2$），修订历史记录 `key_number_changed` / `document_corrected` 审计摘要。

2. **Pipeline.run_once 入口级修订穿透（包含超 72 小时历史事件）**：
   - 在采集与作业持久化层（Durable Job `stage_a_extract`），`input_json` 显式携带 `{'revision_event_id': ...}`。
   - 在 `Pipeline.run_once` 中，即使历史事件发生在 10 天前（远超语义聚类的 72 小时滑动窗口），调度器亦能通过 `target_event_id` 准确定向命中该事件，完成版本升级与内容合并，杜绝历史更正被误拆为独立新事件。

3. **官方否认、撤回与恢复确认状态机 (`trace/event_engine/revision.py`, `trace/event_engine/engine.py`, `trace/settings.yaml`)**：
   - **否认/撤回高优先级**：官方发布否认时，状态确定性流转为 `contradicted`，触发实质更新（`material_update=True`），审计记录记录 `denial_or_retraction` 与 `status_contradicted`。
   - **非权威源防篡改**：当事件处于 `contradicted` 或 `retracted` 状态时，非官方媒体后续报道无法将状态篡改或覆盖回 `reported`/`confirmed`。
   - **恢复确认状态机**：当后续官方权威公告恢复澄清时，状态机允许恢复至 `official_confirmed`，触发实质更新，原因增加 `recovery_to_confirmed` 与 `rumor_to_confirmed`，并纳入 `revision_resend_rules` 复推白名单。

4. **文本变更向量失效与模型身份全生命周期跟踪 (`trace/event_engine/revision.py`, `trace/event_engine/engine.py`, `trace/db/repositories.py`)**：
   - 在 `EventRepo.insert` 和 `EventRepo.update` 中补齐持久化 `embedding_model` 列，杜绝先前仅有 `update_embeddings` 写入而全量对象更新丢失模型身份的问题。
   - 当 `title` 或 `summary` 发生实质修订时，`apply_update` 立即清空 `title_embedding` / `summary_embedding` 与 `embedding_model`（置为 `None`），杜绝陈旧向量残留。
   - `EventEngine._merge_into` 检测到被清空的向量后，使用当前聚类器的 `embedder` 重新生成向量，并将 `embedding_model` 更新为复合身份标识（`{class}:{model_name}:{revision}:{dim}`）。
   - 验证同维不同模型（如均为 256 维但模型名不同）以及同模型不同 revision（如 $v1 \to v2$）在 `_load_window()` 时能够精准通过 `embedding_identity` 识别并自动触发重新向量化与懒回填。

5. **Label-blind 评测隔离与 100 对测试集保真 (`trace/verification/dedup_evaluation.py`, `tests/fixtures/event_pairs_100.json`)**：
   - 纳入受控的 100 对测试集 fixture，其文件哈希严格锁死为 `SHA256: 85664602b4478ffec723446f553ef80cee4725fddd9c1a4bd4afd23e79930cf2`。
   - 预测接口 `predict_pair` 严格实现 Label-blind：输入参数仅为原始文档 `item_a` 与 `item_b`，绝不接受 `expected` 标签；标签仅在外部打分函数 `evaluate_pairs` 中生效。
   - 测试指标实测严格与原规划对齐：**候选召回率 1.0、自动错误合并 0、自动正确合并 30、需要 Verifier 介入 70、`verifier_evaluated: false`**。文档中绝不将候选召回虚报为生产端到端准确率。
   - 为未来真实 Verifier 接入提供单独运行入口，无真实模型接入时严格保持 `pending_real_verifier`。

---

## 2. 自动化验证证据

### 2.1 N06 专项测试套件

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q tests/test_n06_revision_vectors_and_evaluation.py tests/test_dedup_and_revision.py
```
**输出**：
```text
..............                                                           [100%]
14 passed in 16.91s
```

### 2.2 全量 Python 套件验证

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
**输出**：
```text
452 passed, 1 warning in 98.96s (0:01:38)
```

### 2.3 小程序 Node 契约测试

```powershell
node miniprogram/tests/detail_mapping.test.js
node miniprogram/tests/session_research.test.js
```
**输出**：
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

## 3. 验收标准达成对照

| 验收要求 | 落实情况 | 验证证据 |
|---|---|---|
| 同 source_item_id 重放 | 2 份 RawItem 均存库且均关联同事件新版本 | `test_replay_same_source_item_id_retains_two_raw_items_and_advances_version` (PASS) |
| Pipeline 入口修订穿透 | 10 天前历史事件正确修订至 v2，未被聚类窗口阻断 | `test_pipeline_run_once_revision_propagation_past_72h_window` (PASS) |
| 官方否认/撤回/恢复状态机 | 否认不可被媒体篡改，官方恢复确认为 recovery_to_confirmed | `test_status_machine_denial_retraction_and_recovery` (PASS) |
| 文本变更向量清空与重算 | 标题更新清除旧向量，merge 后按当前模型重算回填 | `test_embedding_invalidation_on_title_summary_change` (PASS) |
| 同维不同模型/不同 revision | 基于 `{class}:{model}:{rev}:{dim}` 识别并触发回填 | `test_embedding_model_identity_mismatch_triggers_recalculation`, `test_same_model_different_revision_triggers_recalculation` (PASS) |
| 评测接口 Label-blind | 预测器仅接收文本，标签仅进入评分函数 | `test_100_pair_fixture_checksum_and_label_blind_contract` (PASS) |
| 100 对测试集指标真实保真 | 召回 1.0，自动错合并 0，自动对合并 30，待审核 70，未评测真模型 | `test_100_pair_fixture_checksum_and_label_blind_contract` (PASS) |

---

## 4. 交付清单

- `trace/db/repositories.py` [MODIFIED] - EventRepo.insert/update 持久化 embedding_model
- `trace/event_engine/revision.py` [MODIFIED] - 文本变更清空向量、恢复确认状态机
- `trace/event_engine/engine.py` [MODIFIED] - 向量清空后重算与模型身份持久化
- `trace/settings.yaml` [MODIFIED] - revision_resend_rules 增加 recovery_to_confirmed
- `trace/verification/dedup_evaluation.py` [MODIFIED] - 增加可选 Verifier 运行入口与状态标记
- `tests/fixtures/event_pairs_100.json` [PRESERVED & VERIFIED] - SHA256 校验通过
- `tests/test_n06_revision_vectors_and_evaluation.py` [NEW]
- `verification/product-hardening/next-pass/N06-revision-vectors-eval.md` [NEW]
