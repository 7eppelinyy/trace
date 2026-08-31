"""检查 BIS 已有数据质量（raw_item / event）。"""
import sqlite3

c = sqlite3.connect("data/trace.db")
c.row_factory = sqlite3.Row
print("== BIS raw items sample ==")
for r in c.execute("SELECT title, url FROM raw_item WHERE source_id='src_bis' LIMIT 12"):
    print(r["title"][:60], "|", r["url"][:70])
print()
print("== events by first_source ==")
for r in c.execute(
        "SELECT first_source_id, COUNT(*) n FROM event GROUP BY first_source_id ORDER BY n DESC"):
    print(r["first_source_id"], r["n"])
print()
print("== BIS events sample ==")
for r in c.execute(
        "SELECT event_id, title FROM event WHERE first_source_id='src_bis' LIMIT 15"):
    print(r["event_id"], r["title"][:70])
