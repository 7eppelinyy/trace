"""检查停止的部分 run 在库中留下的状态。"""
import sqlite3

c = sqlite3.connect("data/trace.db")
c.row_factory = sqlite3.Row
print("== raw_item counts ==")
for r in c.execute("SELECT source_id, COUNT(*) n FROM raw_item GROUP BY source_id ORDER BY source_id"):
    print(f"{r['source_id']:24} {r['n']}")
print("event total:", c.execute("SELECT COUNT(*) FROM event").fetchone()[0])
print()
print("== events created today (2026-08-26) by source ==")
for r in c.execute("""SELECT first_source_id, COUNT(*) n FROM event
                      WHERE date(first_seen_at)='2026-08-26'
                      GROUP BY first_source_id"""):
    print(f"{r['first_source_id']:24} {r['n']}")
print()
print("== cursors ==")
for r in c.execute("SELECT source_id, LENGTH(cursor_json) l FROM collector_cursor"):
    print(f"{r['source_id']:24} cursor_bytes={r['l']}")
print()
print("== impacts for today's events ==")
print(c.execute("""SELECT COUNT(*) FROM event_impact i JOIN event e ON e.event_id=i.event_id
                   WHERE date(e.first_seen_at)='2026-08-26'""").fetchone()[0])
