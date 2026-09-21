# T10 交付摘要 · 提醒偏好、日报与渠道状态闭环 (F10 / F27)

> 执行标准：依据执行蓝图 T10 要求，建立服务端持久化全局与单标的偏好、免打扰判定与时区隔离日报缓存，完成小程序状态真实闭环。

---

## 1. 变更范围与整治点

1. **统一偏好模型与原子版本并发控制 (F10)**：
   - 编写迁移 `trace/db/migrations/0017_notification_preferences.sql`：建立 `notification_preference` 表，支持 `user_id`、`security_id` (NULL 为全局，非空为单标的覆盖)、`threshold`、`enabled`、`quiet_start`、`quiet_end`、`channel`、`revision`。
   - `NotificationPreferenceRepo` 实现原子更新与乐观并发锁 `expected_revision`，若版本冲突拒绝修改并抛出 `409 Conflict`，杜绝多设备并发覆写。
   - 实现偏好优先级解析：`单标的覆盖 > 全局偏好 > 默认阈值 (7.0)`。

2. **AlertEngine 免打扰与退订强约束 (F10)**：
   - 接入 `NotificationPreferenceRepo`，在 `evaluate()` 决策链中前置检查：
     - 若 `not pref.enabled`：严格抑制即时推送，统计入 `suppressed`；
     - 若当前用户当地时间处于 `[quiet_start, quiet_end)` 免打扰时段内（支持跨夜 22:00–08:00 与午休区间）：抑制推送；
     - 若 `impact.final_score < pref.threshold`：低于偏好阈值抑制推送。

3. **日报多时区隔离与缓存防串 (F27)**：
   - 重建 `daily_digest` 表，消除旧模式下 `date_str` 单一主键导致的跨时区覆盖；
   - 引入 `cache_key = f"{date_str}:{timezone}:default:v1"`，并建立唯一索引；
   - 确保台北用户 (UTC+8) 与纽约用户 (UTC-4) 即使查询同一本地日期，各自的 24 小时自然日切分与研报摘要独立缓存、互不污染覆盖。

4. **API 与小程序真实端到端状态联动 (F10)**：
   - 新增 REST API `GET /api/v1/preferences` 与 `PUT /api/v1/preferences`；
   - 自选页 `watchlist.js`：加载时并行获取偏好，单标的强提醒开关直连 `PUT /preferences`，操作失败具备 UI 回滚机制；
   - 设置页 `profile.js` & `profile.wxml`：展示真实全局强提醒开关、夜间免打扰区间和时区隔离状态，切换失败自动回滚。

---

## 2. 自动化测试与验证结果

- **测试文件**：`tests/test_preferences_and_digest.py`
  - `test_preference_hierarchy_and_concurrency`: 验证单标的覆盖优于全局偏好，乐观锁并发冲突抛出 409；
  - `test_is_in_quiet_hours`: 验证跨时区跨夜时段与日间时段免打扰判定；
  - `test_alert_engine_suppression`: 验证退订标的与高阈值标的在 AlertEngine 中如实抑制；
  - `test_daily_digest_timezone_cache_isolation`: 验证同一日期不同时区 cache_key 独立存储与隔离；
  - `test_preferences_api_endpoints`: 验证通过 Bearer 会话调用 GET/PUT 接口及 409 冲突。
- **执行结果**：`5 passed in 1.75s` (100% 通过)。

---

## 3. 回滚方案

- 若偏好服务需紧急维护，可降级回全局默认配置，但数据库保留的用户退订状态与免打扰设置不被抹除。
