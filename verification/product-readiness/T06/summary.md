# T06 · 渠道绑定与可靠投递 Outbox 验证报告

## 1. 任务概述
- **任务编号**: T06 (P0)
- **关联问题**: F06（渠道绑定与投递可靠性缺失、用户标识与 Telegram chat_id 混淆）
- **核心目标**: 解耦内部主体身份 (`user_id`) 与接收渠道 (`channel_binding`)，杜绝未绑定小程序用户被当做 Telegram chat_id 误投递；建立可靠异步投递 Outbox 表与具备租约竞争机制的投递 Worker；实现分级重试与退订/撤销自动抑制。

---

## 2. 核心架构与代码变更

### 2.1 数据库迁移与模型
- **SQL 迁移**: `trace/db/migrations/0016_channel_outbox.sql`
  - `channel_binding` 表：管理用户渠道绑定关系 (`binding_id`, `user_id`, `channel_type`, `channel_target`, `is_active`, `verified_at`, `created_at`, `updated_at`)。
  - `alert_outbox` 表：异步任务队列表 (`outbox_id`, `user_id`, `channel_type`, `channel_target`, `event_id`, `impact_id`, `event_version`, `alert_type`, `idempotency_key`, `content_text`, `content_url`, `status`, `retry_count`, `max_retries`, `retry_after`, `lease_until`, `last_error`, `created_at`, `updated_at`)。
  - 兼容迁移现有历史 Telegram 用户数据为内部主体 + `channel_binding` 记录。
- **领域模型与仓库**:
  - `trace/domain/models.py` (`ChannelBinding`, `AlertOutbox`)
  - `trace/db/repositories.py` (`ChannelBindingRepo`, `AlertOutboxRepo`)
  - 支持短事务 + `mode="IMMEDIATE"` 原子批量租约认领 (`claim_batch`)。

### 2.2 投递服务与错误分类重试 (`trace/alerts/delivery_worker.py`)
- 实现 `drain_outbox`：
  - 检查渠道有效性：用户若在入队后撤销绑定/退订，任务自动置为 `suppressed`，保护私密性；
  - 错误分类处理：
    - 429 (Rate Limited)：设置 `retry_after` 延迟重试；
    - 403 / Bot Blocked / Chat Not Found：判定为永久失效，标记任务 `failed` 并自动将对应 `channel_binding` 置为非活跃；
    - 5xx / 网络超时：递增 `retry_count` 并采用指数退避重试，达 `max_retries` 终态标记；
    - 投递成功：落库 `alert_delivery` 结构化回执并标记 outbox 终态为 `sent`。

### 2.3 流水线集成与解耦 (`trace/pipeline.py`)
- `_deliver` 仅将通过评估的事件决策渲染为通知文本后存入 `alert_outbox`，不再直接进行脆弱的同步网络调用。
- 纯小程序端新用户 (`usr_...`) 默认无 Telegram 渠道绑定，跳过直接推送，杜绝向非法目标 ID 发送报错。
- `_run_once` 阶段执行 `drain_outbox`，支持在采集器无新条目的轮次中独立排空与恢复待投递队列。

---

## 3. 测试验证矩阵

### 3.1 专用测试集 `tests/test_outbox.py` (6/6 Passed)
| 用例编号 | 验证场景 | 预期行为 | 测试结果 |
| :--- | :--- | :--- | :--- |
| **OUT-01** | 渠道绑定管理 (`ChannelBindingRepo`) | 支持创建、按用户查询活跃绑定、退订/撤销绑定 | **PASSED** |
| **OUT-02** | 未绑定小程序用户防护 | `usr_` 用户默认无 Telegram 绑定，严禁当做 chat_id 发送 | **PASSED** |
| **OUT-03** | 业务幂等键与原子租约竞争 | 重复入队被过滤；两 Worker 竞争同一任务严格互斥；租约超时自动恢复可认领 | **PASSED** |
| **OUT-04** | 投递成功闭环 | `drain_outbox` 顺利完成发送，outbox 标 sent，生成 `alert_delivery` 回执 | **PASSED** |
| **OUT-05** | 403 / Bot Blocked 致命错误处置 | 自动停用失效的 `channel_binding`，outbox 标 failed，不再死循环重试 | **PASSED** |
| **OUT-06** | 退订/撤销渠道安全抑制 | 入队后若渠道被解绑，发送时自动标记 `suppressed`，绝不外发私密提醒 | **PASSED** |

### 3.2 现有流水线与提醒回归 (58/58 Passed)
- `tests/test_pipeline.py` (6/6 passed)
- `tests/test_alerts.py`, `tests/test_revision_resend.py`, `tests/test_ops.py` (52/52 passed)
