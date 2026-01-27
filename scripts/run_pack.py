from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.artifacts import append_jsonl, configure_logging, init_run_dir, write_meta, write_snapshot  # noqa: E402
from bot.diagnostic import build_diagnostic_snapshot  # noqa: E402


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT)
        return out.decode("utf-8").strip()
    except Exception:
        return None


def _load_config(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_run_config(base_path: Path, run_dir: Path, *, mempool: str) -> Path:
    cfg = _load_config(base_path)
    mempool_on = str(mempool).lower() == "on"
    cfg["mempool_enabled"] = bool(mempool_on)
    if mempool_on:
        cfg["scan_source"] = "hybrid"
    else:
        cfg["scan_source"] = "block"
    out_path = run_dir / "run_config.json"
    out_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return out_path


def _run_subprocess(
    args: list[str],
    *,
    env: Dict[str, str],
    logger: logging.Logger,
    time_limit_s: float,
) -> int:
    proc = subprocess.Popen(
        args,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        assert proc.stderr is not None
        start = time.time()
        while True:
            if proc.poll() is not None:
                break
            if time_limit_s > 0 and (time.time() - start) > (time_limit_s + 30):
                logger.error("run_pack timeout exceeded, terminating")
                proc.terminate()
                break
            line = proc.stdout.readline()
            if line:
                logger.info(line.rstrip())
            err = proc.stderr.readline()
            if err:
                logger.error(err.rstrip())
            time.sleep(0.01)
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return int(proc.returncode or 0)


def _build_candidates_file(run_dir: Path) -> None:
    candidates_path = run_dir / "candidates.jsonl"
    logs_dir = run_dir / "logs"
    for name, kind in (
        ("mempool.jsonl", "mempool"),
        ("trigger_scans.jsonl", "trigger_scan"),
        ("blocks.jsonl", "block_scan"),
    ):
        path = logs_dir / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            append_jsonl(run_dir, "candidates.jsonl", {"kind": kind, **obj})
    if not candidates_path.exists():
        candidates_path.write_text("", encoding="utf-8")


def _write_snapshot_from_logs(
    run_dir: Path,
    config_path: Path,
    *,
    update_reason: str,
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    settings = SimpleNamespace(**(_load_config(config_path)))
    snapshot = build_diagnostic_snapshot(
        log_dir=run_dir / "logs",
        settings=settings,
        session_started_at_s=None,
        current_block=None,
        rpc_stats=None,
        window_s=900,
        update_reason=update_reason,
        run_id=meta.get("run_id"),
        writer_pid=None,
        started_at_ms=meta.get("started_at_ms"),
        last_heartbeat_ts=int(time.time() * 1000),
    )
    snapshot["run_meta"] = {
        "seed": meta.get("seed"),
        "mode": meta.get("mode"),
        "mempool": meta.get("mempool"),
        "blocks": meta.get("blocks"),
        "duration_sec": meta.get("duration_sec"),
        "exit_code": meta.get("exit_code"),
    }
    write_snapshot(run_dir, snapshot)
    return snapshot


def _run_sim(run_dir: Path, *, blocks: int, seed: int, start_block: int = 1000) -> None:
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    blocks_path = logs_dir / "blocks.jsonl"
    for idx in range(blocks):
        entry = {
            "block": int(start_block) + idx,
            "candidates": 0,
            "finished": 0,
            "scan_scheduled": 0,
            "reason_if_zero_scheduled": "sim_mode",
            "seed": int(seed),
        }
        blocks_path.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
    (logs_dir / "mempool.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "trigger_scans.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "mempool_status.json").write_text("{}", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="ArbitoInator run pack")
    parser.add_argument("--mode", default="fork", choices=["fork", "sim"], help="run mode")
    parser.add_argument("--mempool", default="off", choices=["on", "off"], help="enable mempool")
    parser.add_argument("--blocks", type=int, default=0, help="number of blocks to scan")
    parser.add_argument("--from", dest="from_block", type=int, default=0, help="start block (optional)")
    parser.add_argument("--to", dest="to_block", type=int, default=0, help="end block (optional)")
    parser.add_argument("--seed", type=int, default=0, help="random seed")
    parser.add_argument("--out", type=str, default="", help="output run dir (optional)")
    parser.add_argument("--ui", default="off", choices=["on", "off"], help="start UI server")
    parser.add_argument("--time_limit_sec", type=int, default=180, help="time limit")
    args = parser.parse_args()

    base_dir = Path("artifacts")
    run_dir = Path(args.out) if args.out else init_run_dir(base_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = configure_logging(run_dir)

    start_ms = int(time.time() * 1000)
    commit = _git_commit()
    blocks = int(args.blocks or 0)
    if blocks <= 0 and args.from_block and args.to_block and args.to_block >= args.from_block:
        blocks = int(args.to_block - args.from_block + 1)

    meta = {
        "run_id": str(uuid.uuid4()),
        "seed": int(args.seed),
        "mode": args.mode,
        "mempool": args.mempool,
        "blocks": int(blocks),
        "from_block": int(args.from_block or 0),
        "to_block": int(args.to_block or 0),
        "ui": args.ui,
        "time_limit_sec": int(args.time_limit_sec),
        "started_at_ms": start_ms,
        "git_commit": commit,
        "run_dir": str(run_dir),
    }
    write_meta(run_dir, meta)

    exit_code = 0
    ui_proc: Optional[subprocess.Popen[str]] = None
    config_path = PROJECT_ROOT / "bot_config.json"
    run_config = _write_run_config(config_path, run_dir, mempool=args.mempool)

    try:
        if args.ui == "on":
            ui_proc = subprocess.Popen(
                ["node", "ui/server.js"],
                cwd=PROJECT_ROOT,
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            logger.info("UI server started")

        if args.mode == "sim":
            _run_sim(run_dir, blocks=max(1, int(blocks or 1)), seed=int(args.seed), start_block=int(args.from_block or 1000))
            exit_code = 0
        else:
            env = os.environ.copy()
            env["BOT_CONFIG"] = str(run_config)
            env["ARBITOINATOR_CONFIG"] = str(run_config)
            env["RUN_LOG_DIR"] = str(run_dir / "logs")
            env["RUN_ID"] = str(meta.get("run_id"))
            if args.seed:
                env["RUN_SEED"] = str(int(args.seed))
            if blocks > 0:
                env["STOP_AFTER_BLOCKS"] = str(int(blocks))
            if args.time_limit_sec and int(args.time_limit_sec) > 0:
                env["TIME_LIMIT_S"] = str(int(args.time_limit_sec))

            python = sys.executable
            exit_code = _run_subprocess(
                [python, "-u", "fork_test.py"],
                env=env,
                logger=logger,
                time_limit_s=float(args.time_limit_sec),
            )
    finally:
        if ui_proc:
            try:
                ui_proc.terminate()
                ui_proc.wait(timeout=5)
            except Exception:
                try:
                    ui_proc.kill()
                except Exception:
                    pass

    _build_candidates_file(run_dir)

    meta.update(
        {
            "ended_at_ms": int(time.time() * 1000),
            "duration_sec": float((time.time() * 1000 - start_ms) / 1000.0),
            "exit_code": int(exit_code),
            "diagnostic_snapshot": str(run_dir / "diagnostic_snapshot.json"),
        }
    )
    snapshot = _write_snapshot_from_logs(run_dir, run_config, update_reason="shutdown", meta=meta)
    write_meta(run_dir, meta)

    logger.info("run_pack complete: %s", run_dir)
    logger.info("exit_code=%s snapshot=%s", exit_code, run_dir / "diagnostic_snapshot.json")
    logger.info("snapshot_keys=%s", list(snapshot.keys()))
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
