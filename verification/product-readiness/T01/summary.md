# 任务 T01 交付与验收报告：清除事实伪装与无效成功反馈

- **任务 ID**: T01
- **优先级**: P0
- **审阅对应**: F01、F10、F24、F28
- **执行时间**: 2026-09-19 (Asia/Taipei)
- **状态**: 已完成并通过自动化验收

---

## 1. 修复前后对比 (Before / After)

| 场景 / 字段 | 修复前实际表现 (Bug) | 修复后实际表现 (Fixed) | 验证证据 |
|---|---|---|---|
| **事件影响方向转报价** | `bullish` 自动映射为 `change_pct: 1.5%`；无报价时造假 | 缺失行情返回 `null`，前端展示为 `--`；标的方向（利好/利空/中性）作为独立胶囊展示 | 用例 A01 通过；探针输出 `direction_to_quote: null`, `direction_label: "利好"` |
| **未证实事件状态** | `status: "reported"` 描述拼接“官方源证实，已完成权威信源交叉校验” | 如实反映 `status`，媒体报道标注“当前处于媒体报道或待证实阶段，暂无直接官方公告交叉验证” | 用例 A03 通过 |
| **缺失分析评分** | `final_score` 缺失时默认输出 `7.0 分` | 明确标注“综合研判影响分：未评级。需等待后续确定性证据进一步补充” | 用例 A02 通过 |
| **真实事件请求失败** | 真实 ID `EVT-UNAVAILABLE` 失败后回退至苹果 NAND mock 案例 | 保留原 ID，呈现清晰的“加载中断 / 未检索到有效事件”错误状态卡片并提供重试入口，禁止回退 mock | 用例 A04、A05 通过；探针输出 `failed_real_event_fallback: null`, `loadError: true` |
| **自选页面趋势走势图** | 每只标的根据涨跌渲染固定的贝塞尔 SVG 曲线（`SPARK_UP_SVG` / `SPARK_DOWN_SVG`） | 彻底移除固定 SVG 走势曲线，仅展示真实价格与涨跌幅，无时序数据不作伪造图表 | 代码审查与 WXML/JS 清理确认 |
| **自选标的点击跳转** | 任意标的点击硬编码跳转至 `id=apple-nand` 苹果案例 | 跳转改为携带标的进入 Ask 问答页发起该标的产业链深度研判，彻底清除硬编码 demo 依赖 | WXML/JS 代码重构确认 |
| **微信推送与消息设置** | 客户端 `setData` 伪状态后弹窗“已开启推送” | 弹窗如实告知用户“微信推送通道接入中，暂未开通对外即时推送” | JS/WXML 交互整改确认 |
| **本地缓存清理** | 硬编码展示 `84 MB`，定时器延迟后仅重置为 `0` | 调用 `wx.getStorageInfoSync()` 动态获取实际 KB 占用，并真实清理非凭据缓存 | JS/WXML 交互整改确认 |
| **首页刷新提示** | 固定提示“核验官方源中... / 已刷新 14 家官方源池” | 提示“同步最新事件中... / 已更新至 HH:mm”，如实反映客户端拉取行为 | JS 交互整改确认 |
| **问答空内容处理** | 空响应自动兜底“当前标的处于常规波动区间，未见突发异动” | 转换为 `insufficient_evidence`，提示“推演引擎未检索到相关事实证据或未能生成有效推演结论” | `ask.js` 整改确认 |

---

## 2. 自动化测试结果

执行测试脚本：
```powershell
node miniprogram/tests/detail_mapping.test.js
```

输出：
```text
=== Running T01 Detail Mapping & Authenticity Tests ===
✔ A01 Passed: No fake change_pct generated for bullish impact without quote
✔ A02 Passed: Missing score does not default to 7.0
✔ A03 Passed: Unconfirmed event does not claim official verification or cross check
✔ A04 Passed: Failed real event does not fallback to Apple NAND mock
✔ A05 Passed: getMockDetailById does not silently fallback to Apple NAND

=== All T01 Detail Mapping Tests Passed! ===
```

运行蓝图 11.1 隔离探针：
```json
{
  "direction_to_quote": null,
  "direction_label": "利好",
  "unconfirmed_claim": "Audit unconfirmed event。当前处于媒体报道或待证实阶段，暂无直接官方公告交叉验证。",
  "missing_score": "综合研判影响分：未评级。需等待后续确定性证据进一步补充。",
  "failed_real_event_fallback": null,
  "loadError": true,
  "errorMessage": "未检索到该事件详情或该事件已被归档"
}
```

---

## 3. 回滚方案
如需临时回滚，可检出相关小程序页面文件（`miniprogram/pages/detail/*`, `pages/watchlist/*`, `pages/ask/*`, `pages/index/*`, `pages/profile/*`, `utils/mock.js`, `utils/api.js`），但严禁在生产版本重新引入虚构数据兜底。
