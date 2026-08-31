# MVP V1 真实纵向切片 — 已知限制

状态：**STOP_TRACE_MVP_V1_TELEGRAM_TOKEN_MISSING**（同时 LLM Key 未配置）
日期：2026-08-26

## 1. 凭据阻塞（唯一阻塞项）

| 缺失项 | 影响 | 解除方式 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 无法实际投递；无回执记录 | 创建 `.env` 并填入 BotFather Token |
| `TELEGRAM_DEFAULT_CHAT_ID` | 无默认接收用户；`alerts_eligible=0` | 填入自己的 chat id |
| `OPENAI_API_KEY` | production 模式停止（`STOP_LLM_KEY_MISSING`）；offline 只能规则降级 | 填入兼容 OpenAI 接口的 Key |

真实数据链路（采集→去重→Event→图谱候选→评分→渲染）已全部真实验证；
只有"实际发送到 Telegram"与"真实 LLM 分析"两步等待凭据。

## 2. 数据源限制

- **Micron IR**：官方 RSS 端点限流 429，`src_micron_ir` 保持禁用（不用第三方内容冒充）。
- **SanDisk IR**：官方 RSS 不可达（404/超时），`src_sndk_ir` 保持禁用。
- **SSE/SZSE**：采用任务书允许的路线B——巨潮公告的市场过滤适配器，默认禁用以避免与 `src_cninfo` 重复；需要按市场独立健康跟踪时显式启用。
- **行情**：美股/A股均为 Mock（显式标记 `market_data_mode=mock`，消息中已注明"模拟数据"）。Alpaca Key 未配置。
- **Embedding**：未安装 sentence-transformers，降级为 hash embedder——跨语言语义聚类能力丧失，仅确定性去重有效。
- **中芯国际案例**：巨潮检索到的首条公告为其港股公告（沪深港两地披露），A股事件发现不受影响。

## 3. 分析限制（无 LLM 时）

- 方向只能输出 `uncertain`（保守正确）；真实方向判断需配置 LLM。
- "为什么重要"为图谱路径说明，非事实级归因。
- 事件 `event_status` 由来源类型确定性映射（官方来源→official_confirmed）。

## 4. 工程限制

- 非 git 仓库（基线即如此），本轮未初始化版本控制。
- SQLite 单库；无后台守护进程，`run-once` 为验收入口。
- production 门禁已实现并经测试覆盖，但当前环境从未在 production 模式下真实跑完全程（缺凭据）。

## 5. 凭据齐备后的验收步骤

```
python -m trace.main doctor          # 全部 OK
python -m trace.main run-once        # 第一次：真实采集 + 真实 LLM + Telegram 投递
python -m trace.main run-once        # 第二次：验证零重复
```
