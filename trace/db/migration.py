"""migration 机制：按文件名序号顺序执行 *.sql，并记录已应用版本。"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from trace.db.connection import Database

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def apply_migrations(db: Database) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        # SQLite official 12-step table-rebuild procedure: disable foreign keys outside
        # the transaction, run DDL, verify foreign_key_check before commit, and re-enable.
        db.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with db.transaction(mode="IMMEDIATE"):
                if db.query_one("SELECT 1 FROM schema_version WHERE version=?", (version,)):
                    continue
                logger.info("applying migration %s", version)
                pending = ''
                for char in sql_file.read_text(encoding="utf-8"):
                    pending += char
                    if char == ';' and sqlite3.complete_statement(pending):
                        db.conn.execute(pending)
                        pending = ''
                if pending.strip():
                    db.conn.execute(pending)
                fk_violations = db.conn.execute("PRAGMA foreign_key_check;").fetchall()
                if fk_violations:
                    raise sqlite3.IntegrityError(f"Migration {version} produced foreign key violations: {fk_violations}")
                db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        finally:
            db.conn.execute("PRAGMA foreign_keys=ON")
