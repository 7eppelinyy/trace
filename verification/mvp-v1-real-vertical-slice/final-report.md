# MVP V1 真实纵向切片 — 最终报告

日期：2026-08-26
最终状态：**STOP_TRACE_MVP_V1_TELEGRAM_TOKEN_MISSING**
（真实数据链路已打通并验证；仅 Telegram 凭据与 LLM Key 未配置导致最后两步无法实际执行）

---

## 1. 测试

- 基线：**24 passed**
- 最终：**115 passed**（新增 91 个，覆盖任务书 §14 全部要求；实时网络测试单独 `live` 标记）

## 2. Collector 真实状态

| Collector | 状态 |
|---|---|
| SEC EDGAR | ✅ REAL（本轮真实采集 80 条 8-K/10-Q/10-K；合规 UA/限流/游标/增量） |
| NVIDIA IR | ✅ REAL（官方 RSS 真实采集） |
| Micron IR | ⏸ Disabled（官方 RSS 429 限流，拒绝第三方冒充） |
| SanDisk IR | ⏸ Disabled（官方 RSS 不可达） |
| CNINFO 巨潮 | ✅ REAL（本轮真实采集 A股公告，含中芯国际等） |
| SSE / SZSE | 路线B：巨潮市场过滤适配器（默认禁用，防重复抓取） |
| Fed / BIS / Federal Register | ✅ REAL |
| Policy / Industry Media | 保留（非本轮关键路径） |
| 美股行情（Alpaca） | MOCK（显式标记，无 Key） |
| A股行情 | MOCK（显式标记） |

## 3. SNDK / MU / NVDA CIK 核验（SEC company_tickers.json 官方权威表）

| ticker | 本地 | 官方 | 结果 |
|---|---|---|---|
| SNDK | 0002023554 | 0002023554 | ✅ MATCH（已修正 seed） |
| MU | 0000723125 | 0000723125 | ✅ MATCH |
| NVDA | 0001045810 | 0001045810 | ✅ MATCH |

命令：`python -m trace.main verify-securities`

## 4. 实际采集到的真实事件

- **案例A（美股）**：`EVT-20260825-3dc67608b433`「SNDK 10-K」，来源 SEC EDGAR，
  原文 `https://www.sec.gov/Archives/edgar/data/2023554/...`，status=official_confirmed
- **案例B（A股）**：`EVT-20260825-1c90cbb1172b`「中芯国际 2026 中期业绩公布」，
  来源巨潮资讯，原文 `http://static.cninfo.com.cn/finalpage/2026-08-14/1225471507.PDF`

关键字段（规则降级，无 LLM）：direction=uncertain、directness=direct、
final_score≈7.4-7.5、confidence=0.4、analysis_mode=rule_based_degraded、
market_data_mode=mock。渲染文本见 `us-rendered-alert.txt` / `cn-rendered-alert.txt`。

## 5. Telegram 实际发送

**未执行**——无 Token/chat id。任务书规定不得用 Mock 绕过，
`telegram-delivery-receipts.json` 如实记录零回执与解锁方式。
投递解析/幂等已由单元测试完整覆盖。

## 6. 重复运行幂等（真实数据库）

| 轮次 | run_id | new | duplicate | events_created | alerts_sent |
|---|---|---|---|---|---|
| 1 | run-eb09ab06ac | 233 | 21 | 233 | 0 |
| 2 | run-6e214edfc3 | 0 | 86 | 0 | 0 |
| 3 | run-6725f8ec0d | 0 | 86 | 0 | 0 |
| 4 | run-4a969ed209 | 0 | 86 | 0 | 0 |

第 2-4 轮：新增重复 RawItem=0、新增重复 Event=0、新增重复投递=0。

## 7. 用户需要填写的环境变量（`.env`）

```
TRACE_MODE=production
OPENAI_API_KEY=<your-key>
OPENAI_MODEL=gpt-4o-mini          # 可选
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_DEFAULT_CHAT_ID=<your chat id>
SEC_CONTACT_EMAIL=<your email>    # SEC 公平访问
```

## 8. 启动命令

```
python -m trace.main doctor          # 环境诊断
python -m trace.main run-once        # 单次真实流水线（验收）
python -m trace.main verify-securities
python -m trace.main bot             # Telegram Bot（long polling）
```

## 9. 证据文件（verification/mvp-v1-real-vertical-slice/）

baseline-audit.md / doctor-output.txt / pytest-output.txt / live-source-smoke.json /
us-real-event.json / cn-real-event.json / us-rendered-alert.txt / cn-rendered-alert.txt /
telegram-delivery-receipts.json / duplicate-run-proof.json / source-health.json /
known-limitations.md / final-report.md
