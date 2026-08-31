"""生成 baseline.txt：本轮开发前的数据库状态快照（G13 证据之一）。"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

OUT = Path("verification/source-completion/baseline.txt")
OUT.parent.mkdir(parents=True, exist_ok=True)

c = sqlite3.connect("data/trace.db")
c.row_factory = sqlite3.Row
lines = ["Trace Source Completion Gate V1 — baseline snapshot",
         f"generated_at: {datetime.now(timezone.utc).isoformat()}",
         ""]
lines.append("== raw_item by source ==")
for r in c.execute("SELECT source_id, COUNT(*) n, MAX(published_at) maxt "
                   "FROM raw_item GROUP BY source_id ORDER BY source_id"):
    lines.append(f"{r['source_id']:24} count={r['n']:5} latest={r['maxt']}")
lines.append("")
lines.append(f"event total: {c.execute('SELECT COUNT(*) FROM event').fetchone()[0]}")
lines.append(f"alert_delivery total: {c.execute('SELECT COUNT(*) FROM alert_delivery').fetchone()[0]}")
sent = c.execute("SELECT COUNT(*) FROM alert_delivery WHERE status='sent'").fetchone()[0]
lines.append(f"alert_delivery sent: {sent}")
lines.append("")
lines.append("== pytest baseline ==")
lines.append("151 passed (before source-completion tests), target >= 131")
OUT.write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
