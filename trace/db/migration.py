"""migration 机制：按文件名序号顺序执行 *.sql，并记录已应用版本。"""

from __future__ import annotations

import logging
from pathlib import Path

from trace.db.connection import Database

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_migrations(db: Database) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {row["version"] for row in db.query("SELECT version FROM schema_version")}

    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        logger.info("applying migration %s", version)
        script = sql_file.read_text(encoding="utf-8")
        db.conn.executescript(script)
        db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
