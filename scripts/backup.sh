#!/usr/bin/env bash
set -euo pipefail
python3 "$(dirname "$0")/db_admin.py" backup --source "${TRACE_DB_PATH:-data/trace.db}" --directory "${TRACE_BACKUP_DIR:-data/backups}" --keep "${TRACE_BACKUP_KEEP:-7}"
