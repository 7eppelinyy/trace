#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <backup> <NEW-target-db-path>" >&2
    exit 1
fi
python3 "$(dirname "$0")/db_admin.py" restore --source "$1" --target "$2"
