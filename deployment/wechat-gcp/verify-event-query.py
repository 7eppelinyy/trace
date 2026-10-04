"""Read-only production query probe for the candidate repository module."""
import importlib.util
import signal
import time

from trace.db.connection import Database

signal.alarm(20)
spec = importlib.util.spec_from_file_location("trace_candidate_repositories", "/tmp/trace-repositories-20261004.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
db = Database("/opt/trace/data/trace.db")
started = time.monotonic()
events, total, _ = module.EventRepo(db).paginate_events(limit=6, enforce_display=True)
elapsed = time.monotonic() - started
print(f"EVENT_QUERY_SECONDS={elapsed:.3f} VISIBLE_TOTAL={total} PAGE_COUNT={len(events)}")
db.close()
assert elapsed < 5, "Event query still too slow for client timeout"
