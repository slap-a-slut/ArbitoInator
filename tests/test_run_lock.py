import json
import os
import subprocess
import sys
import time
from pathlib import Path

from bot.run_lock import RunLock


def test_run_lock_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "bot_run.lock"
    lock = RunLock(lock_path)

    ok, reason, payload = lock.acquire(run_id="run-a", pid=os.getpid())
    assert ok and reason is None
    assert payload and payload.get("run_id") == "run-a"

    # Simulate another alive process holding the lock.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
    try:
        lock_path.write_text(json.dumps({"pid": proc.pid, "run_id": "other"}), encoding="utf-8")
        ok2, reason2, _ = lock.acquire(run_id="run-b", pid=os.getpid())
        assert not ok2
        assert reason2 == "already_running"
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass


def test_run_lock_stale_recovery(tmp_path: Path) -> None:
    lock_path = tmp_path / "bot_run.lock"
    lock = RunLock(lock_path)
    lock_path.write_text(json.dumps({"pid": 9999999, "run_id": "stale"}), encoding="utf-8")
    ok, reason, payload = lock.acquire(run_id="fresh", pid=os.getpid())
    assert ok and reason is None
    assert payload and payload.get("run_id") == "fresh"
