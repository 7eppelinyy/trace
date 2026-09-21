# N05 行情质量与稳定查询专项验收报告

> 日期：2026-09-21 (Asia/Taipei)  
> 责任阶段：N05 - 把行情质量与稳定查询完整传到客户端 (P1)  
> 运行环境：`.acceptance/clean-env/Scripts/python.exe` (Python 3.11), Node v24.16.0

---

## 1. 任务范围与落地概况

根据 `docs/Trace_后续修正精确执行计划_2026-09-21.md` §8 (N05)，本阶段将行情质量时效、慢行情解耦、游标稳定分页及十万级查询性能全面收口：

1. **Quote 数据契约与质量状态全链路传递 (`trace/collectors/market_data/base.py`, `trace/api/schemas.py`)**：
   - 在 `Quote` 领域模型中明确并补齐了 `market_timestamp`、`fetched_at`、`source`、`currency`、`is_delayed`、`change_basis`，并添加动态计算的 `quality` 属性（支持 `real`、`delayed`、`stale`、`unknown_timestamp`、`invalid_timestamp`、`mock`、`unavailable`）。
   - 在 API 契约层同步扩充了 `IndexQuoteItem`、`EventSecurityChip`、`WatchlistEntryItem`、`WatchlistSearchItem`，杜绝“仅后端类扩展而 API 输出遗漏”的脱节现象。
   - 在 `PaginationMeta` 中保持 `cursor` 与 `next_cursor` 双向兼容，确保前后端与第三方工具调用的一致性。

2. **慢行情与事件列表解耦 (`trace/collectors/market_data/confirmation.py`, `trace/api/routers/events.py`)**：
   - 在 `MarketConfirmer` 中实现非阻塞的 `cached_quote` 与 `cached_quotes` 方法，仅读取本地时效内内存快照，绝不触发对外部供应商的同步网络 I/O。
   - `/api/v1/events` 列表端点切换至 `cached_quotes`。即使外部市场供应商遭受极端慢网络（例如 `time.sleep(5.0)` 模拟注入），事件列表亦能在毫秒级响应（测试实测 <0.5s），未命中缓存的标的 chip 返回 `quality="unavailable"`，绝不阻塞事件正文的下发。

3. **分页与总数计算性能收口 (`trace/db/repositories.py`)**：
   - 优化 `EventRepo.paginate_events`：在游标模式 (`cursor is not None`) 下直接跳过全表总数 `COUNT` 聚合，保持游标分页 $O(1)$ 的流式查询优势；在无表连接时剔除无意义的 `DISTINCT` 去重，显著降低 SQLite 内存消耗与执行耗时。

4. **缓存退避保真与过期脱敏 (`trace/api/routers/market.py`, `trace/api/routers/watchlist.py`)**：
   - 修复 `/api/v1/market/indices` 缓存回退逻辑：当从降级快照回退时，严格保持原有的 `delayed`、`stale`、`unknown_timestamp` 质量标签，绝不伪造或升级为 `cached` 或 `real`。
   - 设定 30 分钟缓存最长存活期（`_CACHE_MAX_AGE_SECONDS = 1800`），超过此期限的不可信快照将清空价格与涨跌幅（`price=None, change_pct=None`）并将状态诚实置为 `unavailable`。

5. **日历出处与 2027 年份审慎标注 (`trace/market_time/holidays.yaml`)**：
   - 明确标注中美两地休市日历的官方核验出处（美股引证 NYSE Rule 7.2，A 股引证上交所规则与国务院办公厅放假通知）。
   - 明确标注 2027 年休市日历为前瞻投影（preliminary projections），提示待每年 11–12 月两地官方正式公布后再行最终核验更新。

6. **十万级数据索引基准与游标契约 (`trace/db/migrations/0030_event_cursor_and_quote_perf.sql`, `tests/test_quote_quality_and_events_decoupling.py`)**：
   - 追加入库迁移 `0030_event_cursor_and_quote_perf.sql`，建立复合索引 `idx_event_first_seen ON event(first_seen_at DESC, event_id DESC)`。
   - 通过 `EXPLAIN QUERY PLAN` 自动化校验，确保游标查询精准命中该复合索引，杜绝全表扫描（SCAN TABLE）与临时 B-Tree 排序（TEMP B-TREE）。
   - 在真实生成 100,000 条合成事件记录的数据库中，连续 20 次游标分页跳转的 **P95 延时仅为 0.35–0.75ms**（远优于 15ms 指标）。
   - 严格测试并确立了 `first_seen_membership_latest_content` 分页契约：翻页期间即使正文发生修订（版本升级为 2），成员依据 `first_seen_at` 保持稳定不丢不重，返回正文为最新内容；尾页 `next_cursor` 严格为 `None` 且 `has_more` 为 `False`；非法/篡改游标严格抛出 422。

---

## 2. 自动化验证证据

### 2.1 N05 专项测试套件

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q tests/test_quote_quality_and_events_decoupling.py
```
**输出**：
```text
......                                                                   [100%]
6 passed, 1 warning in 4.78s
```

### 2.2 全量 Python 套件验证

```powershell
.\.acceptance\clean-env\Scripts\python.exe -m pytest -q
```
**输出**：
```text
445 passed, 1 warning in 85.29s (0:01:25)
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
| Quote 契约字段全链路传递 | 领域模型与 REST API 均具备 7 项时效元数据与质量属性 | `test_quote_contract_and_quality_property` (PASS) |
| 慢行情不阻断事件正文 | 事件列表仅取缓存，外部 provider 5s 延迟下列表 <0.5s 返回 | `test_events_list_decoupled_from_slow_quotes` (PASS) |
| 游标分页契约与稳定性 | 满足 `first_seen_membership_latest_content`，抗翻页中修订，尾页游标为 null | `test_cursor_pagination_contract_and_revision_stability` (PASS) |
| 复合时间戳防碰撞 | 同一秒多事件无遗漏、无重复分页 | `test_cursor_collision_on_identical_first_seen_at` (PASS) |
| 10 万事件查询索引与 P95 | `idx_event_first_seen` 索引生效，无 TEMP B-TREE，P95 < 1ms (< 15ms 门禁) | `test_100k_events_index_query_plan_and_latency` (PASS) |
| 缓存退避质量保真 | stale/delayed 绝不升级为 cached/real，超龄隐藏数值 | `test_cache_fallback_does_not_upgrade_degraded_quality` (PASS) |
| 假日日历来源审查 | 官方出处已注释，2027 标记为未定案前瞻投影 | `trace/market_time/holidays.yaml` |

---

## 4. 交付清单

- `trace/db/migrations/0030_event_cursor_and_quote_perf.sql` [NEW]
- `trace/collectors/market_data/base.py` [MODIFIED]
- `trace/collectors/market_data/confirmation.py` [MODIFIED]
- `trace/api/schemas.py` [MODIFIED]
- `trace/api/routers/events.py` [MODIFIED]
- `trace/api/routers/market.py` [MODIFIED]
- `trace/api/routers/watchlist.py` [MODIFIED]
- `trace/db/repositories.py` [MODIFIED]
- `trace/market_time/holidays.yaml` [MODIFIED]
- `tests/test_quote_quality_and_events_decoupling.py` [NEW]
- `verification/product-hardening/next-pass/N05-market-quotes.md` [NEW]
