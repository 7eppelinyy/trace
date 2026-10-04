"""Install a verified candidate only when the production source is unchanged."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

target = Path("/opt/trace/trace/db/repositories.py")
expected = "5960bce102aeb1b33b5edf5a09fd48773c61ba18095fa90d331c89169058615b"
assert hashlib.sha256(target.read_bytes()).hexdigest() == expected, "Production source changed; inspect before deployment"
candidate = Path("/tmp/trace-repositories-20261004.py")
compile(candidate.read_text(encoding="utf-8"), str(candidate), "exec")
backup = Path("/opt/trace/.deploy/repositories.before-20261004.py")
assert not backup.exists(), "Deployment backup already exists"
shutil.copy2(target, backup)
temporary = target.with_suffix(".py.deploy")
shutil.copy2(candidate, temporary)
os.chmod(temporary, target.stat().st_mode & 0o777)
os.replace(temporary, target)
subprocess.run(["sudo", "systemctl", "restart", "trace-api"], check=True, timeout=120)
print("EVENT_QUERY_FIX_DEPLOYED")
