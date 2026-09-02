"""SQLite + WAL 连接管理。

Phase 1 使用 SQLite；Repository 层保持 DB 无关的 Domain Model，
后续迁移 PostgreSQL 时不需要重写领域对象。

bot 模式下 pipeline 线程与 Telegram 事件循环线程并发写同一库文件：
WAL 允许多读单写，但写者相遇时默认立即抛 database is locked ——
必须设置 busy_timeout 让后到写者短暂等待而不是直接失败。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import local

BUSY_TIMEOUT_MS = 5000


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
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._local.conn = conn
        return conn

    def _in_txn(self) -> bool:
        return getattr(self._local, "in_txn", False)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        if not self._in_txn():
            self.conn.commit()
        return cur

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        cur = self.conn.executemany(sql, seq)
        if not self._in_txn():
            self.conn.commit()
        return cur

    @contextmanager
    def transaction(self):
        """把一批 execute/executemany 合并为单次 commit（原子 + 批量 fsync）。

        Repository 层调用方无需感知：事务期间 execute 不再逐条 commit。
        可嵌套；内层块交给外层事务统一提交。
        """
        if self._in_txn():
            yield self.conn
            return
        self._local.in_txn = True
        try:
            self.conn.execute("BEGIN")
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self._local.in_txn = False

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
