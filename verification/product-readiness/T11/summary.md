# T11 交付摘要 · 事件查询性能、稳定分页与客户端请求治理 (F13 / F24)

> 执行标准：依据执行蓝图 T11 要求，消除全表扫描与静默 1000 条截断，建立稳定快照定界语义与 SQL 批量加载，实施客户端请求治理（长词拦截、主数据优先、并发去重与网络异常重试）。

---

## 1. 变更范围与整治点

1. **SQL 级过滤与稳定快照分页下推 (F13)**：
   - 编写迁移 `trace/db/migrations/0018_event_query_indexes.sql`：建立 `event(last_updated_at DESC, event_id DESC)`、`event_impact(event_id, final_score DESC)`、`event_impact(security_id, final_score DESC)` 等复合索引；
   - `EventRepo.paginate_events(...)`：废除内存切片和静默 1000 条上限截断，将 `market`、`min_score`、`snapshot_ts` 条件下推至 SQL `COUNT` 与 `LIMIT / OFFSET`；
   - 支持 `snapshot_ts` 快照锚定语义：首屏请求生成稳定时间戳，后续触底翻页锁定该时间戳（`e.last_updated_at <= snapshot_ts`），彻底解决用户浏览中并发新增事件导致的列表下移和条目漂移重现。

2. **消除 N+1 查询与批量 Impact 关联 (F13)**：
   - `EventImpactRepo.list_by_events(event_ids)`：单次 SQL (`WHERE event_id IN (...)`) 批量查出当页所有关联证券与评分，替代旧有逐条循环点查，查询次数从 `O(N)` 降至 `O(1)`。

3. **行情异常弹性降级与搜索长词治理 (F24)**：
   - `trace/api/routers/events.py`：行情查询 (`ctx.confirmer.quotes`) 增加防御性隔离与容错，第三方行情超时或异常时不阻断事件主体列表的渲染与返回；
   - `trace/api/routers/watchlist.py`：设置 `q = Query(..., max_length=50)`，超长恶意请求直接阻断返回 HTTP 422；在搜索外部 Smartbox 前，优先查询本地 `Security` 主数据（代码、中文名、英文名、别名），命中有保障且显著降低外部行情 API 消耗。

4. **客户端请求合并、防抖与失败重试 (F24)**：
   - `miniprogram/utils/api.js`：新增 `inFlightRequests` Map 缓存，毫秒级相同的 GET 请求自动合并为同一个网络 Promise，避免页面初次挂载与多组件并发时重复发包；
   - `miniprogram/pages/watchlist/watchlist.js`：引入 `searchGen` 世代序列计数器，快速打字切换时丢弃过期的异步旧响应，防止结果错位；在 `onUnload` 中销毁搜索防抖定时器；
   - `miniprogram/pages/ask/ask.js`：增加显式异常错误卡片与重试按钮 (`retryAsk`)，网络异常时允许用户一键发起重新提问。

---

## 2. 自动化测试与验证结果

- **测试文件**：`tests/test_pagination_and_query_governance.py`
  - `test_sql_pagination_and_filtering`: 验证基础分页、市场过滤（US/CN）、最低评分过滤（min_score）在 SQL 下推下的准确性；
  - `test_snapshot_pagination_prevents_page_drift`: 验证在并发插入新事件场景下，携带 `snapshot_ts` 的分页严格保持首尾连贯，不发生漂移；
  - `test_batch_impacts_eliminates_n_plus_one`: 验证 `list_by_events` 批量关联 impact 数据；
  - `test_events_api_pagination_and_resilience`: 验证 API 响应包含 `snapshot_ts` 分页元数据，且超长搜索词触发 422 验证异常。
- **执行结果**：`4 passed in 1.52s` (100% 通过)。
- **全量测试套件验证**：`357 passed in 43.34s` (全库测试零回归)。

---

## 3. 回滚方案

- 索引与 SQL 翻页完全向下兼容，若需单机调试仅需移除 `snapshot_ts` 传参即可回到实时浮动翻页。
