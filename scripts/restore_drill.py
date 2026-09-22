"""SQLite 备份恢复与一致性演练脚本 (T14 / F20)。

自动执行：
1. 校验源数据库状态；
2. 执行一致性快照备份；
3. 将备份恢复至独立的临时演练库；
4. 执行 PRAGMA 完整性检查与源/目标全表行数比对；
5. 输出详细演练报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from trace.db.backup import create_backup, restore_backup, verify_database_integrity
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.data.seed import load_all_seeds


def run_drill(source_db_path: Path, backup_dir: Path, drill_target_path: Path) -> dict:
    start_time = datetime.now(timezone.utc)
    print(f"[*] Starting Restore Drill at {start_time.isoformat()}...")
    print(f"[*] Source DB: {source_db_path}")
    print(f"[*] Backup Dir: {backup_dir}")
    print(f"[*] Drill Target DB: {drill_target_path}")

    if not source_db_path.is_file():
        raise FileNotFoundError('Restore drill source is missing; a seeded empty database is not evidence')
    if drill_target_path.exists() or drill_target_path.resolve() == source_db_path.resolve():
        raise ValueError('Drill target must be a new, separate path')

    # 1. 验证源数据库完整性
    src_verify = verify_database_integrity(source_db_path)
    print(f"[+] Source DB Integrity: {src_verify['integrity_check']}, Foreign Keys: {src_verify['foreign_key_check']}")

    # 2. 执行快照备份
    backup_file = create_backup(source_db_path, backup_dir)
    print(f"[+] Backup Created: {backup_file} (Size: {backup_file.stat().st_size} bytes)")

    # 3. 恢复至独立演练数据库
    # Compare against the frozen backup, not a live source that may keep changing.
    src_verify = verify_database_integrity(backup_file)
    restore_result = restore_backup(backup_file, drill_target_path, verify=True)
    target_verify = restore_result["verification"]
    print(f"[+] Target DB Restored & Verified: {target_verify['integrity_check']}")

    # 4. 数据一致性比对 (源库 vs 恢复库)
    mismatches = []
    for tbl, count in src_verify["table_counts"].items():
        restored_cnt = target_verify["table_counts"].get(tbl)
        if count != restored_cnt:
            mismatches.append(f"Table {tbl}: source={count}, restored={restored_cnt}")

    if src_verify["latest_event_time"] != target_verify["latest_event_time"]:
        mismatches.append(
            f"Latest event time mismatch: source={src_verify['latest_event_time']}, "
            f"restored={target_verify['latest_event_time']}"
        )

    if src_verify['table_hashes'] != target_verify['table_hashes']:
        mismatches.append('Table content hashes differ')
    drill_success = (not mismatches and src_verify['integrity_check'] == 'ok'
                     and target_verify['integrity_check'] == 'ok'
                     and src_verify['foreign_key_check'] == 'ok'
                     and target_verify['foreign_key_check'] == 'ok'
                     and not src_verify['missing_core_tables'] and not target_verify['missing_core_tables'])
    end_time = datetime.now(timezone.utc)
    duration_s = round((end_time - start_time).total_seconds(), 3)

    report = {
        "drill_status": "PASS" if drill_success else "FAIL",
        "timestamp": start_time.isoformat(),
        "duration_seconds": duration_s,
        "rto_seconds": duration_s,
        "rpo_basis": "snapshot_at_backup_creation",
        "latest_event_time": src_verify.get("latest_event_time"),
        "drill_mode": "local_isolated_recovery_verification",
        "source_db": str(source_db_path),
        "backup_file": str(backup_file),
        "drill_target": str(drill_target_path),
        "source_verification": src_verify,
        "target_verification": target_verify,
        "mismatches": mismatches,
    }

    print("\n================ DRILL RESULT ================")
    print(f"Status: {report['drill_status']}")
    print(f"Duration: {duration_s}s")
    if mismatches:
        print("[!] Found discrepancies:")
        for m in mismatches:
            print(f"    - {m}")
    else:
        print("[+] Perfect match: all table counts and integrity checks matched exactly.")
    print("==============================================\n")

    # Clean up drill temp file
    if drill_target_path.exists():
        drill_target_path.unlink()
        print(f"[*] Cleaned up temporary drill DB: {drill_target_path}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Trace SQLite restore drill")
    parser.add_argument("--source", default="data/trace.db", help="Path to source SQLite database")
    parser.add_argument("--backup-dir", default="data/backups", help="Directory where backups are stored")
    parser.add_argument("--target", default="data/restore_drill_tmp.db", help="Temporary database path for drill")
    parser.add_argument("--output-json", default=None, help="Save report to JSON file")
    args = parser.parse_args()

    res = run_drill(Path(args.source), Path(args.backup_dir), Path(args.target))
    if args.output_json:
        out_p = Path(args.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[*] Saved report to {out_p}")

    if res["drill_status"] != "PASS":
        sys.exit(1)
