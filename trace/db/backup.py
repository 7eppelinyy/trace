"""SQLite 每日备份与轮换。

- 用 sqlite3 backup API 做一致性快照（对 WAL 活动库安全，不锁写）；
- 文件名 trace-YYYYMMDD-HHMMSS.db，同一天只备一份（长驻循环每轮检查）；
- 超出 keep 份数的旧备份自动删除（文件名字典序即时间序）。
"""

from __future__ import annotations

import logging
import sqlite3
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def create_backup(db_path: str | Path, backup_dir: str | Path,
                  keep: int = 7, now: datetime | None = None) -> Path:
    """对 db_path 做一致性快照并轮换旧备份，返回备份文件路径。"""
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    if keep < 1:
        raise ValueError('At least one backup must be retained')
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(timezone.utc))
    dest = backup_dir / f"trace-{ts.strftime('%Y%m%d-%H%M%S-%f')}.db"
    pending = dest.with_suffix('.partial')

    src = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(str(pending))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    try:
        checks = verify_database_integrity(pending)
        _require_valid(checks)
        os.replace(pending, dest)
    finally:
        if pending.exists():
            pending.unlink()
    kept = rotate(backup_dir, keep)
    logger.info("backup created: %s (kept %d)", dest, len(kept))
    return dest


def rotate(backup_dir: str | Path, keep: int = 7) -> list[Path]:
    """删除超出 keep 份数的旧备份，返回保留列表（新→旧）。"""
    backup_dir = Path(backup_dir)
    if keep < 1:
        raise ValueError('At least one backup must be retained')
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


def quarantine_restored_outbox(db_path: str | Path) -> dict:
    """对恢复后的数据库执行 Outbox 隔离：将 sending 状态转换为 ambiguous，防止旧库回滚后突发重复通知。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert_outbox'")
        if not cur.fetchone():
            return {"quarantined_count": 0}
        cur.execute(
            "UPDATE alert_outbox SET status='ambiguous', "
            "last_error='restored_from_backup_quarantine', lease_until=NULL "
            "WHERE status='sending'"
        )
        count = cur.rowcount
        conn.commit()
        logger.info("Quarantined %d sending outbox items in restored database %s", count, db_path)
        return {"quarantined_count": count}
    finally:
        conn.close()


def restore_backup(backup_path: str | Path, target_db_path: str | Path,
                   *, verify: bool = True, quarantine_outbox: bool = False) -> dict:
    """从备份文件恢复数据库到目标路径，并执行数据一致性与完整性校验。"""
    backup_path = Path(backup_path)
    target_db_path = Path(target_db_path)

    if not backup_path.exists():
        raise FileNotFoundError(f"backup file not found: {backup_path}")
    if target_db_path.exists() or backup_path.resolve() == target_db_path.resolve():
        raise ValueError('Restore requires a new target path; never overwrite a running database')
    if verify:
        _require_valid(verify_database_integrity(backup_path))

    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(backup_path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        dst = sqlite3.connect(str(target_db_path))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    result = {
        "status": "success",
        "backup_path": str(backup_path),
        "target_db_path": str(target_db_path),
        "restored_at": datetime.now(timezone.utc).isoformat(),
    }

    if verify:
        verification = verify_database_integrity(target_db_path)
        result["verification"] = verification
        _require_valid(verification)

    if quarantine_outbox:
        q_result = quarantine_restored_outbox(target_db_path)
        result["quarantine"] = q_result

    logger.info("database restored: %s -> %s (verified=%s, quarantine=%s)",
                backup_path, target_db_path, verify, quarantine_outbox)
    return result


def verify_database_integrity(db_path: str | Path) -> dict:
    """对指定 SQLite 数据库执行 PRAGMA 完整性检查与关键表行数统计。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.DatabaseError as exc:
        return {
            "integrity_check": f"database_error: {exc}",
            "foreign_key_check": "error",
            "table_counts": {},
            "table_hashes": {},
            "missing_core_tables": ["event", "raw_item", "user", "schema_version"],
            "latest_event_time": None,
        }
    conn.row_factory = sqlite3.Row
    try:
        # 1. 完整性检查
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA integrity_check;")
            rows = cur.fetchall()
            integrity = "ok" if len(rows) == 1 and rows[0][0] == "ok" else "; ".join(r[0] for r in rows)
        except sqlite3.DatabaseError as exc:
            integrity = f"database_error: {exc}"

        # 2. 外键一致性检查
        try:
            cur.execute("PRAGMA foreign_key_check;")
            fk_errors = cur.fetchall()
            fk_status = "ok" if len(fk_errors) == 0 else f"{len(fk_errors)} violations"
        except sqlite3.DatabaseError as exc:
            fk_status = f"database_error: {exc}"

        # 3. 关键业务表记录统计
        try:
            key_tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            counts = {}
            hashes = {}
            for tbl in key_tables:
                quoted = '"' + tbl.replace('"','""') + '"'
                cols = [row[1] for row in conn.execute(f'PRAGMA table_info({quoted})')]
                order = ','.join('"' + c.replace('"','""') + '"' for c in cols)
                digest = hashlib.sha256()
                count = 0
                for row in conn.execute(f'SELECT * FROM {quoted} ORDER BY {order}'):
                    payload = json.dumps(list(row), ensure_ascii=False, separators=(',', ':'),
                                         default=lambda b: {'bytes_hex': bytes(b).hex()}).encode()
                    digest.update(len(payload).to_bytes(8,'big'))
                    digest.update(payload)
                    count += 1
                counts[tbl], hashes[tbl] = count, digest.hexdigest()
        except sqlite3.DatabaseError as exc:
            return {
                "integrity_check": integrity if integrity != "ok" else f"database_error: {exc}",
                "foreign_key_check": "error",
                "table_counts": {},
                "table_hashes": {},
                "missing_core_tables": ["event", "raw_item", "user", "schema_version"],
                "latest_event_time": None,
            }

        # 4. 最新事件时间戳
        latest_event_time = None
        try:
            cur.execute("SELECT MAX(event_time) AS max_t FROM event")
            r = cur.fetchone()
            latest_event_time = r["max_t"] if r else None
        except sqlite3.OperationalError:
            pass

        return {
            "integrity_check": integrity,
            "foreign_key_check": fk_status,
            "table_counts": counts,
            "table_hashes": hashes,
            "missing_core_tables": sorted({'event','raw_item','user','schema_version'} - set(key_tables)),
            "latest_event_time": latest_event_time,
        }
    finally:
        conn.close()


def _require_valid(checks):
    if checks['integrity_check'] != 'ok' or checks['foreign_key_check'] != 'ok' or checks['missing_core_tables']:
        raise ValueError('Database validation failed: integrity, foreign keys or required tables')
