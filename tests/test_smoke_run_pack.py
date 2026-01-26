import json
import subprocess
import sys
from pathlib import Path


def test_run_pack_smoke(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    out_dir = tmp_path / "smoke_run"
    cmd = [
        sys.executable,
        str(root / "scripts" / "run_pack.py"),
        "--mode",
        "sim",
        "--mempool",
        "off",
        "--blocks",
        "2",
        "--seed",
        "123",
        "--out",
        str(out_dir),
        "--time_limit_sec",
        "5",
    ]
    proc = subprocess.run(cmd, cwd=root, check=False, capture_output=True, text=True)
    assert proc.returncode == 0
    assert out_dir.exists()
    assert (out_dir / "diagnostic_snapshot.json").exists()
    assert (out_dir / "candidates.jsonl").exists()
    meta = json.loads((out_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert meta.get("exit_code") == 0
