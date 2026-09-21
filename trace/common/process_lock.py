"""Cross-platform single-instance process lock using native OS file locking.

This guarantees that only one pipeline runner or bot instance can run
against a given database directory at any time, preventing duplicate
job processing, concurrent cursor corruption, or split-brain clustering.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class SingleInstanceLock:
    """Acquires an exclusive OS-level file lock on a designated lockfile.

    If the holding process terminates or crashes unexpectedly, the OS kernel
    automatically releases the file descriptor lock immediately.
    """

    def __init__(self, lock_path: str | Path, name: str = "pipeline_runner"):
        self.lock_path = Path(lock_path)
        self.name = name
        self._file = None
        self._locked = False

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._file = open(self.lock_path, "a+", encoding="utf-8")
            if os.name == "nt":
                import msvcrt
                # Seek to start and lock 1 byte non-blocking
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._file.seek(0)
            self._file.truncate()
            self._file.write(f"pid={os.getpid()}\nname={self.name}\n")
            self._file.flush()
            self._locked = True
            logger.info("Acquired single-instance lock for %s (PID %s) at %s",
                        self.name, os.getpid(), self.lock_path)
            return True
        except (BlockingIOError, OSError, PermissionError) as exc:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            self._locked = False
            raise RuntimeError(
                f"Another {self.name} is already active (lockfile: {self.lock_path})"
            ) from exc

    def release(self) -> None:
        if self._locked and self._file:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
            except Exception as exc:
                logger.warning("Error releasing single-instance lock: %s", exc)
            finally:
                self._file = None
                self._locked = False
                logger.info("Released single-instance lock for %s", self.name)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
