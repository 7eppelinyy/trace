#!/usr/bin/env bash
# VM reboot 前后状态记录（持久化证明）
cd /opt/trace
echo "=== events count ==="
.venv/bin/python -c "import sqlite3;db=sqlite3.connect('data/trace.db');print('events:',db.execute('SELECT COUNT(*) FROM event').fetchone()[0]);print('raw_items:',db.execute('SELECT COUNT(*) FROM raw_item').fetchone()[0]);print('cursors:',db.execute('SELECT COUNT(*) FROM collector_cursor').fetchone()[0]);print('source_health:',db.execute('SELECT COUNT(*) FROM source_health').fetchone()[0])"
echo "=== uptime ==="
uptime
