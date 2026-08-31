"""验收前清理：把本轮新源的状态重置到干净的"首次接入"。

清理范围（仅限本轮 Source Completion 涉及的新源与脏数据）：
    - 今天（2026-08-26）部分 run 产生的 raw_item / event / 关联行
    - 昨天（2026-08-25）BIS 解析器抓进的导航垃圾（25 条）
    - 重置新源游标（让 bootstrap suppression 从头完整演示）
保留：
    - src_sec_edgar / src_cninfo 游标（已稳定来源，任务书 §2 不重做）
    - alert_delivery 全量保留（其中 2 条为部分 run 的真实投递，
      作为"历史/新事件噪声"观测证据）
"""
import sqlite3

DB = "data/trace.db"
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# ---- 1. 删除今天的 raw_item 与关联 event ----
today_event_ids = [r["event_id"] for r in c.execute(
    "SELECT event_id FROM event WHERE date(first_seen_at)='2026-08-26'")]
c.execute("DELETE FROM raw_item WHERE date(fetched_at)='2026-08-26'")
if today_event_ids:
    q = ",".join("?" * len(today_event_ids))
    for table in ("event_impact", "event_source", "event_revision"):
        c.execute(f"DELETE FROM {table} WHERE event_id IN ({q})", today_event_ids)
    c.execute(f"DELETE FROM event WHERE event_id IN ({q})", today_event_ids)
print(f"deleted today: raw_items + {len(today_event_ids)} events")

# ---- 2. 删除 BIS 导航垃圾（非 press-release 链接的 raw_item 与其事件） ----
garbage = [r["raw_item_id"] for r in c.execute(
    "SELECT raw_item_id FROM raw_item WHERE source_id='src_bis' "
    "AND url NOT LIKE '%/press-release/%'")]
garbage_events = [r["event_id"] for r in c.execute(
    "SELECT DISTINCT event_id FROM raw_item WHERE raw_item_id IN (%s) "
    "AND event_id IS NOT NULL" % ",".join("?" * len(garbage)), garbage)] \
    if garbage else []
if garbage:
    c.execute("DELETE FROM raw_item WHERE raw_item_id IN (%s)"
              % ",".join("?" * len(garbage)), garbage)
if garbage_events:
    q = ",".join("?" * len(garbage_events))
    for table in ("event_impact", "event_source", "event_revision"):
        c.execute(f"DELETE FROM {table} WHERE event_id IN ({q})", garbage_events)
    c.execute(f"DELETE FROM event WHERE event_id IN ({q})", garbage_events)
print(f"deleted BIS garbage: {len(garbage)} raw_items, {len(garbage_events)} events")

# ---- 3. 重置新源游标（保留 sec_edgar / cninfo） ----
keep = ("src_sec_edgar", "src_cninfo")
reset = [r["source_id"] for r in c.execute(
    "SELECT source_id FROM collector_cursor")]
reset = [s for s in reset if s not in keep]
if reset:
    c.execute("DELETE FROM collector_cursor WHERE source_id IN (%s)"
              % ",".join("?" * len(reset)), reset)
print(f"reset cursors: {reset}")

# ---- 4. 重置新源健康状态（重新走 bootstrap） ----
new_sources = ("src_sndk_ir", "src_micron_ir", "src_commerce",
               "src_miit", "src_mofcom", "src_bis",
               "src_federal_register", "src_fed", "src_nvidia_ir")
c.execute("UPDATE source_health SET last_success_at=NULL, last_failure_at=NULL, "
          "last_error=NULL, last_error_category=NULL, consecutive_failures=0, "
          "last_item_at=NULL, last_http_status=NULL "
          "WHERE source_id IN (%s)" % ",".join("?" * len(new_sources)),
          new_sources)

c.commit()

# ---- 验证 ----
print("\n== after cleanup ==")
for r in c.execute("SELECT source_id, COUNT(*) n FROM raw_item GROUP BY source_id"):
    print(f"{r['source_id']:24} {r['n']}")
print("event total:", c.execute("SELECT COUNT(*) FROM event").fetchone()[0])
print("alert_delivery total (保留):",
      c.execute("SELECT COUNT(*) FROM alert_delivery").fetchone()[0])
c.close()
