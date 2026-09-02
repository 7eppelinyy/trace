"""SQLite 每日备份与轮换。

- 用 sqlite3 backup API 做一致性快照（对 WAL 活动库安全，不锁写）；
- 文件名 trace-YYYYMMDD-HHMMSS.db，同一天只备一份（长驻循环每轮检查）；
- 超出 keep 份数的旧备份自动删除（文件名字典序即时间序）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def create_backup(db_path: str | Path, backup_dir: str | Path,
                  keep: int = 7, now: datetime | None = None) -> Path:
    """对 db_path 做一致性快照并轮换旧备份，返回备份文件路径。"""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(timezone.utc))
    dest = backup_dir / f"trace-{ts.strftime('%Y%m%d-%H%M%S')}.db"

    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    kept = rotate(backup_dir, keep)
    logger.info("backup created: %s (kept %d)", dest, len(kept))
    return dest


def rotate(backup_dir: str | Path, keep: int = 7) -> list[Path]:
    """删除超出 keep 份数的旧备份，返回保留列表（新→旧）。"""
    backup_dir = Path(backup_dir)
    files = sorted(backup_dir.glob("trace-*.db"), reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
            logger.info("backup rotated out: %s", old.name)
        except OSError as exc:
            logger.warning("backup cleanup failed for %s: %s", old, exc)
    return files[:keep]


def latest_backup(backup_dir: str | Path) -> Path | None:
    """最近一份备份（无则 None），供 /status 展示。"""
    files = sorted(Path(backup_dir).glob("trace-*.db"), reverse=True)
    return files[0] if files else None
