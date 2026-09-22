"""Safe argument-based backup/restore CLI; never interpolate paths into Python code."""
import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trace.db.backup import create_backup, restore_backup

parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command',required=True)
backup = sub.add_parser('backup')
backup.add_argument('--source',required=True)
backup.add_argument('--directory',required=True)
backup.add_argument('--keep',type=int,default=7)
restore = sub.add_parser('restore')
restore.add_argument('--source',required=True)
restore.add_argument('--target',required=True,help='A NEW database path; overwriting a running database is refused')
restore.add_argument('--quarantine-outbox',action='store_true',default=False,help='Quarantine in-flight sending outbox items to ambiguous to prevent duplicate notifications')
if __name__ == '__main__':
    args = parser.parse_args()
    if args.command == 'backup':
        print(create_backup(args.source,args.directory,keep=args.keep))
    else:
        print(json.dumps(restore_backup(args.source,args.target,quarantine_outbox=args.quarantine_outbox),ensure_ascii=False,indent=2))
