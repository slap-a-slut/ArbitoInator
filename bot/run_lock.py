from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            if not self.path.exists():
                return None
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            return None
        return None

    def acquire(self, *, run_id: str, pid: Optional[int] = None) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        if pid is None:
            pid = os.getpid()
        pid = int(pid)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Fast path: try atomic create
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = {"pid": pid, "run_id": str(run_id), "started_at_ms": int(time.time() * 1000)}
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
            return True, None, payload
        except FileExistsError:
            pass
        except Exception as exc:
            return False, f"lock_error:{exc}", None

        existing = self._read() or {}
        existing_pid = int(existing.get("pid") or 0)
        existing_run = str(existing.get("run_id") or "")
        if existing_pid == pid:
            return True, None, existing
        if existing_pid and _is_pid_alive(existing_pid):
            return False, "already_running", existing

        # Stale lock: replace
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = {"pid": pid, "run_id": str(run_id), "started_at_ms": int(time.time() * 1000), "recovered": True}
            os.write(fd, json.dumps(payload).encode("utf-8"))
            os.close(fd)
            return True, None, payload
        except Exception as exc:
            return False, f"lock_error:{exc}", None

    def release(self, *, run_id: Optional[str] = None, pid: Optional[int] = None) -> bool:
        if pid is None:
            pid = os.getpid()
        pid = int(pid)
        existing = self._read()
        if not existing:
            return True
        if run_id is not None and str(existing.get("run_id") or "") != str(run_id):
            return False
        if pid and int(existing.get("pid") or 0) not in (0, pid):
            return False
        try:
            self.path.unlink(missing_ok=True)
            return True
        except Exception:
            return False
