"""SQLite + WAL 连接管理。

Phase 1 使用 SQLite；Repository 层保持 DB 无关的 Domain Model，
后续迁移 PostgreSQL 时不需要重写领域对象。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import local


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = local()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        cur = self.conn.executemany(sql, seq)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


_default_db: Database | None = None


def get_database(path: str | Path) -> Database:
    global _default_db
    if _default_db is None or _default_db.path != str(path):
        _default_db = Database(path)
    return _default_db
