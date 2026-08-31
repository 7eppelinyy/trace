# 供需景气信号（Supply-Demand Signal）Gate — 验收报告

- **日期**: 2026-08-31
- **结论**: `READY_SUPPLY_DEMAND_SIGNAL_V1`
- **触发**: 把 a9「财报季方法」——业绩大增公司财报里出现
  「供不应求 / 涨价 / 高景气 / 供给偏紧 / 需求旺盛」等措辞 → 重点关注——
  固化成一个**确定性、可测试、脱离 LLM 主观打分**的评分维度。

---

## 1. 设计

新增事件级 `supply_demand` 信号，与"市场确认"并列作为 `final_score` 的**对称微调**。

```
final_score = clamp( base_score
                   + 0.15 * (market_confirmation - 5)     # 既有
                   + 0.10 * (supply_demand - 5),          # 新增（权重可配）
                   1, 10)
```

供给-需求信号由 `trace/scoring/signals.py` 产出：
```
raw = (#bullish 命中) - (#bearish 命中)
raw > 0 : score = 6 + min(3, raw)  → 7 / 8 / 9（偏紧/涨价/缺货）
raw < 0 : score = 4 - min(3, -raw) → 3 / 2 / 1（过剩/降价/需求走弱）
raw = 0 : score = 5（中性）
```

单一真相源：`trace/data/supply_demand_signals.yaml`
- 49 个利多词（供不应求/供给偏紧/缺货/涨价/价格中枢/合约价/现货价/需求旺盛/高景气/订单饱满/量价齐升/shortage/tight supply/sold out/price hike…）
- 35 个利空词（供过于求/产能过剩/降价/价格战/以价换量/需求疲软/砍单/库存高企/oversupply/glut/price cut/weak demand…）
- `focus_guidance`（282 字）→ 拼进 Stage A 系统提示，指引 LLM 把供需事实抽进 `facts`/`key_numbers`

哲学对齐：**禁止 LLM 凭感觉打 1–10 分**；本信号只用词表匹配，方向仍由 Stage B 对每个证券独立判定。

## 2. 改动

| 文件 | 内容 |
|---|---|
| `trace/data/supply_demand_signals.yaml` | 利多/利空词表 + Stage A 抽取指引（单一真相源） |
| `trace/scoring/signals.py` | `detect_supply_demand()`、`load_signals()`、`focus_guidance()`、`SupplyDemandSignal` |
| `trace/scoring/engine.py` | `ScoreInput.supply_demand`（默认 5 中性）；`final_score` 新增对称微调；权重 `scoring.supply_demand_weight` 默认 0.10 |
| `trace/ai/pipeline.py` | `analyze_event` 里对事件算一次信号 → 套用到每个 impact 的 `ScoreInput`；有命中时记 INFO 日志 |
| `trace/ai/extractor.py` | Stage A 系统提示追加 `focus_guidance`（空指引时零改动） |
| `tests/test_scoring.py` | +3（supply_demand 维度在 final_score / score() / 默认中性） |
| `tests/test_supply_demand_signals.py` | +10（词表一致性、多空匹配、分数映射、focus 拼接、不虚构、对称） |

## 3. 验证

- 本地 pytest：**162 基线 → 175 passed**（+13）
- 服务器 pytest：175 passed（已同步）
- 线上模块加载：bullish 49 / bearish 35 / focus_guidance 282 字 ✅
- 真实事件端到端：`Kingboard Laminates 因缺货上调 CCL 价格` → `bullish 7.0`（命中 shortage）✅
- 服务重启 `active`，无回归

## 4. 必须强调的警告（已写入 yaml 头注释）

1. **它是供需拐点的弱强度代理，不是估值判断。** 对周期股，「价格中枢上涨/供给偏紧/缺货」
   这些词**恰恰常出现在景气度顶部**，之后是产能投放→价格回落→戴维斯双杀。绝不能被当"买点"信号。
2. **不单独决定方向**——方向（bullish/bearish）由 Stage B 对每个证券判定，本信号只在同方向内微调强度。
3. **权重刻意小（0.10）**，最大对 final_score 的偏移为 ±0.50（sd=10→+0.5，sd=1→-0.4）。
4. 词表**对称**（既识别偏紧也识别过剩）是为了尽量避免"只看到利多词"的单边误判。
5. 它**不是买卖信号**，只是引擎里一个客观文字强度维度；是否告警仍由 alert_rule 阈值（默认 7.0）决定。

## 5. 后续（可选）

- 将 `supply_demand_score/direction/matches` 持久化到 `event_impact`（需一次 schema 迁移），
  便于在 Telegram 告警模板里直接展示命中的关键词。
- 接入「首次出现 vs 重复出现」判别：同一措辞第三次重复出现时应视为共识已满、信号衰减，
  以缓解周期股顶部误判（当前未做，做需事件级历史比对）。
