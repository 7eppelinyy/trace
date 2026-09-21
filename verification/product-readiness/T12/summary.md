# T12 交付摘要 · 行情语义、交易日历和跨周末补算 (F15—F18)

> 执行标准：依据执行蓝图 T12 要求，严格区分交易所时间戳与抓取时间戳，分离日涨跌与 15m 变动，修复 Alpaca 最近 bar 倒序选取，建立多年度交易日历、提前收盘（Early Close）与超出覆盖安全降级机制，建立基于交易开盘时刻的持久化补算调度（突破 48h 限制，实现跨周末周五盘后事件平滑补算）。

---

## 1. 变更范围与整治点

1. **Quote 语义区分与快照存证增强 (F15)**：
   - 编写迁移 `trace/db/migrations/0019_market_rescore_schedule.sql`：
     - 为 `market_snapshot` 增加 `change_pct_day`、`currency`、`source`、`is_delayed`、`market_ts`；
     - 为 `event_impact` 增加 `next_eligible_at`、`expires_at` 及复合索引；
   - `Quote` 数据类全面升级：明确区分 `market_timestamp`（交易所撮合/报价时间）与 `fetched_at`（本地系统抓取解析时间）；
   - `change_pct_day`（基于前收盘价的全日涨跌幅）与 `change_pct_15m`（严格 15 分钟区间变动）字段物理分离；
   - 彻底废除旧模式下在 API 中用 15m 替代日涨跌的静默降级行为（`events.py`、`watchlist.py`、`ask.py` 全部重构为独立字段展示）。

2. **Alpaca 15m Bar 倒序选择与时间对齐 (F16)**：
   - `AlpacaProvider._change_pct_15m`：改用 `sort="desc", limit=2`，确保获取的是紧邻请求时刻的最近两根 bar，并按正序重新排布计算区间变化，消除从 2 小时前早期 bar 截取的错误逻辑。

3. **跨多年度交易日历、特殊时段与覆盖监测 (F18)**：
   - 扩展 `trace/market_time/holidays.yaml`：覆盖 2024–2027 多年度中美市场休市日；
   - 建模提前收盘时段（Early Close，如美股黑色星期五、平安夜提前至 13:00 收盘）；
   - 准确建模 A 股午休（11:30–13:00 不处于开盘交易状态）；
   - `MarketCalendar.check_coverage(market, days_ahead=60)`：提前至少 60 天监测日历有效覆盖；
   - 超出已知覆盖范围时抛出 `CalendarUnavailableError`，在 `MarketConfirmer` 中触发安全降级返回 `calendar_unavailable`，中性评分 5.0，绝不伪造市场确认。

4. **因果限定与价格背景保守折半收敛 (F17)**：
   - `MarketConfirmation` 区分 `event_anchored`（事件后精准 15m 反应）与 `price_context`（全日价格背景）；
   - 当仅具备全日价格背景时，对加减分进行保守折半收敛（`5.0 + (raw - 5.0) * 0.5`），并在说明模板明确标识“非事件因果验证”。

5. **持久化下一次开盘时刻与跨周末补算闭环 (F17)**：
   - 在休市时段发生事件（如周五 18:00 ET 盘后公告），`MarketConfirmer` 自动推导 `next_eligible_at`（周一 09:30 ET）与 `expires_at`（周一 09:30 + 24h = 周二 09:30 ET）并持久化；
   - `EventImpactRepo.list_pending_confirmation`：根据 `i.next_eligible_at <= datetime('now')` 驱动补算调度，彻底解决旧版固定 48 小时查询裁掉周末事件的问题；
   - `EventImpactRepo.expire_stale_pending_confirmations()`：在补算前自动将超过 `expires_at` 的未决记录标记为 `reaction_window_expired`，防止脏数据无限轮询。

6. **指数缓存最大年龄平滑降级 (F15)**：
   - `trace/api/routers/market.py`：设置指数缓存分级：
     - `< 300s`：`cached`
     - `300s – 1800s`：`stale`
     - `> 1800s`：`unavailable`（数值置空，不以陈旧历史价格误导用户）。

---

## 2. 自动化测试与验证结果

- **专属测试文件**：`tests/test_market_semantics_and_calendar.py`
  - `test_market_calendar_rules_and_early_close`: 验证提前收盘、午休、60 天覆盖监测与超出范围异常；
  - `test_quote_provenance_and_snapshot_persistence`: 验证 Quote 完整存证与日/15m 字段隔离落库；
  - `test_alpaca_15m_bar_selection`: 验证 Alpaca 倒序两根 bar 提取与涨跌计算；
  - `test_market_confirmation_gates_and_causality`: 验证周五盘后事件抑制、周一开盘折半收敛与日历异常安全降级；
  - `test_cross_weekend_rescore_schedule_and_expiry`: 验证 next_eligible_at 持久化与过期自动清理；
  - `test_index_cache_age_degradation`: 验证指数缓存 3 级渐进降级策略。
- **关联测试覆盖**：`tests/test_cn_market_data.py`, `tests/test_market_confirmation.py`, `tests/test_market_data.py` (全部同步适配更新并通过)。
- **执行结果**：31 个关联行情与确认测试全数通过。
- **全量回归套件**：`363 passed in 46.92s` (全库 0 失败，无退化)。

---

## 3. 回滚方案

- 调度字段与日历覆盖边界为纯向上兼容扩展，如需紧急降级可通过将 `scoring.rescore_on_market_open` 置为 `false` 暂停开盘自动补算。
