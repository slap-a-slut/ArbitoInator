import json
import subprocess
import sys
from pathlib import Path


def test_golden_runs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    fixtures_path = root / "tests" / "fixtures" / "golden" / "golden_runs.json"
    runs = json.loads(fixtures_path.read_text(encoding="utf-8"))

    for run in runs:
        out_dir = tmp_path / run["name"]
        cmd = [
            sys.executable,
            str(root / "scripts" / "run_pack.py"),
            "--mode",
            run.get("mode", "sim"),
            "--mempool",
            run.get("mempool", "off"),
            "--blocks",
            str(run.get("blocks", 1)),
            "--seed",
            str(run.get("seed", 0)),
            "--out",
            str(out_dir),
            "--time_limit_sec",
            "5",
        ]
        proc = subprocess.run(cmd, cwd=root, check=False, capture_output=True, text=True)
        assert proc.returncode == 0

        snapshot_path = out_dir / "diagnostic_snapshot.json"
        assert snapshot_path.exists()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        for key in run.get("expect_keys", []):
            assert key in snapshot

        blocks_path = out_dir / "logs" / "blocks.jsonl"
        if blocks_path.exists():
            lines = blocks_path.read_text(encoding="utf-8").splitlines()
            assert len(lines) == int(run.get("blocks", 1))
