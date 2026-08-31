#!/usr/bin/env bash
# 采集 GCP 部署验收证据（不含任何 secret）→ /opt/trace/verification/google-cloud-deployment-v1/
set -e
OUT=/opt/trace/verification/google-cloud-deployment-v1
mkdir -p "$OUT"
cd /opt/trace

echo "=== 1. VM / gcloud 状态 ==="
{
  echo "instance: trace-vm"
  echo "zone: us-central1-a"
  uname -a
  lsb_release -d
  uptime
  systemctl is-active trace-run
  systemctl is-enabled trace-run
} > "$OUT/vm-status.txt" 2>&1

echo "=== 2. python / venv ==="
{
  .venv/bin/python --version
  .venv/bin/pip list 2>/dev/null | head -50
} > "$OUT/python-venv.txt" 2>&1

echo "=== 10. 持久化（磁盘上的 DB） ==="
{
  ls -la data/
  .venv/bin/python -c "import sqlite3;db=sqlite3.connect('data/trace.db');print('tables:',[r[0] for r in db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])"
  .venv/bin/python -c "import sqlite3;db=sqlite3.connect('data/trace.db');print('raw_item:',db.execute('SELECT COUNT(*) FROM raw_item').fetchone()[0],'event:',db.execute('SELECT COUNT(*) FROM event').fetchone()[0],'cursors:',db.execute('SELECT COUNT(*) FROM collector_cursor').fetchone()[0],'source_health:',db.execute('SELECT COUNT(*) FROM source_health').fetchone()[0])"
} > "$OUT/persistence-proof.txt" 2>&1

echo "=== 15. 资源占用（e2-micro） ==="
{
  echo "--- free -m ---"; free -m
  echo "--- df -h / ---"; df -h /
  echo "--- top snapshot ---"; top -bn1 | head -20
  echo "--- swap ---"; swapon --show 2>/dev/null || echo "no swap"
  echo "--- trace process RSS ---"; ps -o pid,rss,vsz,cmd -C python3 2>/dev/null || true
} > "$OUT/resource-usage.txt" 2>&1

echo "=== 7. Source Health ==="
.venv/bin/python -c "
import sqlite3,json
db=sqlite3.connect('data/trace.db');db.row_factory=sqlite3.Row
print(json.dumps([dict(r) for r in db.execute('SELECT * FROM source_health ORDER BY source_id')],ensure_ascii=False,indent=1))
" > "$OUT/source-health.json" 2>&1

echo "=== .env 权限与去代理确认 ==="
{
  ls -la /opt/trace/.env
  echo "--- proxy keys present in server .env (expect none) ---"
  grep -iE '^(HTTPS_PROXY|HTTP_PROXY|ALL_PROXY|NO_PROXY)=' /opt/trace/.env && echo "PROXY_FOUND" || echo "NO_PROXY_CONFIG"
  echo "--- secret vars masked (names only) ---"
  grep -oE '^[A-Z_]+=' /opt/trace/.env
} > "$OUT/env-security.txt" 2>&1

echo "=== 14. 日志 secret 扫描（应无命中） ==="
{
  echo "scan journal + run logs for API key / bot token patterns..."
  FOUND=0
  for f in /tmp/run1.log /tmp/run2.log /tmp/doctor.log /tmp/pytest.log; do
    if [ -f "$f" ]; then
      if grep -Ei '(sk-[a-zA-Z0-9]{20,}|[0-9]{10}:AA[A-Za-z0-9_-]{30,})' "$f" >/dev/null 2>&1; then
        echo "SECRET_FOUND in $f"; FOUND=1
      fi
    fi
  done
  sudo journalctl -u trace-run --no-pager 2>/dev/null > /tmp/journal_dump.txt || true
  if grep -Ei '(sk-[a-zA-Z0-9]{20,}|[0-9]{10}:AA[A-Za-z0-9_-]{30,})' /tmp/journal_dump.txt >/dev/null 2>&1; then
    echo "SECRET_FOUND in journal"; FOUND=1
  fi
  [ "$FOUND" -eq 0 ] && echo "NO_SECRET_IN_LOGS_OR_JOURNAL"
} > "$OUT/secret-scan.txt" 2>&1

echo EVIDENCE_DONE
