"""检查部分 run 的完整状态，决定清理范围。"""
import sqlite3

c = sqlite3.connect("data/trace.db")
c.row_factory = sqlite3.Row

print("== today's events by first_source ==")
for r in c.execute("""SELECT first_source_id, COUNT(*) n FROM event
                      WHERE date(first_seen_at)='2026-08-26'
                      GROUP BY first_source_id ORDER BY n DESC"""):
    print(f"{r['first_source_id']:24} {r['n']}")

print("\n== today's events with impacts ==")
print(c.execute("""SELECT COUNT(DISTINCT e.event_id) FROM event e
                   JOIN event_impact i ON i.event_id=e.event_id
                   WHERE date(e.first_seen_at)='2026-08-26'""").fetchone()[0])

print("\n== today's raw_items by source ==")
for r in c.execute("""SELECT source_id, COUNT(*) n FROM raw_item
                      WHERE date(fetched_at)='2026-08-26'
                      GROUP BY source_id ORDER BY n DESC"""):
    print(f"{r['source_id']:24} {r['n']}")

print("\n== BIS raw_items: press-release vs garbage ==")
pr = c.execute("SELECT COUNT(*) FROM raw_item WHERE source_id='src_bis' "
               "AND url LIKE '%/press-release/%'").fetchone()[0]
total = c.execute("SELECT COUNT(*) FROM raw_item WHERE source_id='src_bis'").fetchone()[0]
print(f"press-release={pr} garbage={total-pr} total={total}")

print("\n== cursors ==")
for r in c.execute("SELECT source_id, LENGTH(cursor_json) l FROM collector_cursor"):
    print(f"{r['source_id']:24} cursor_bytes={r['l']}")

print("\n== alert_delivery today ==")
print(c.execute("SELECT COUNT(*) FROM alert_delivery WHERE date(sent_at)='2026-08-26'"
                ).fetchone()[0])

print("\n== human_review today ==")
print(c.execute("SELECT COUNT(*) FROM human_review WHERE date(created_at)='2026-08-26'"
                ).fetchone()[0])

print("\n== event_revision today ==")
print(c.execute("SELECT COUNT(*) FROM event_revision WHERE date(created_at)='2026-08-26'"
                ).fetchone()[0])
