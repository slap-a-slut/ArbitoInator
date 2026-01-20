from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_LINES = 50


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl_tail(path: Path, *, max_lines: int) -> List[Dict[str, Any]]:
    if max_lines <= 0:
        return []
    if not path.exists():
        return []
    try:
        data = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for line in data[-max_lines:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def build_assistant_pack(
    *,
    log_dir: Path,
    config_path: Path,
    max_lines: int = DEFAULT_LINES,
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    diagnostic = _read_json(log_dir / "diagnostic_snapshot.json")
    cfg = _read_json(config_path)
    mempool_lines = _read_jsonl_tail(log_dir / "mempool.jsonl", max_lines=max_lines)
    trigger_lines = _read_jsonl_tail(log_dir / "trigger_scans.jsonl", max_lines=max_lines)
    block_lines = _read_jsonl_tail(log_dir / "blocks.jsonl", max_lines=max_lines)

    pipeline = diagnostic.get("pipeline") if isinstance(diagnostic, dict) else None
    last_trigger = diagnostic.get("last_trigger") if isinstance(diagnostic, dict) else None

    env_flags = {
        "SIM_PROFILE": str(os.getenv("SIM_PROFILE", "")),
        "DEBUG_FUNNEL": str(os.getenv("DEBUG_FUNNEL", "")),
        "GAS_OFF": str(os.getenv("GAS_OFF", "")),
        "FIXED_GAS_UNITS": str(os.getenv("FIXED_GAS_UNITS", "")),
        "ENABLE_MULTIDEX": str(os.getenv("ENABLE_MULTIDEX", "")),
        "SCAN_SOURCE": str(os.getenv("SCAN_SOURCE", "")),
        "MEMPOOL_ENABLED": str(os.getenv("MEMPOOL_ENABLED", "")),
    }

    return {
        "schema_version": 1,
        "generated_at_ms": now_ms,
        "diagnostic_snapshot": diagnostic,
        "config": cfg,
        "pipeline": pipeline,
        "last_trigger": last_trigger,
        "logs": {
            "mempool": mempool_lines,
            "trigger_scans": trigger_lines,
            "blocks": block_lines,
        },
        "env_flags": env_flags,
    }


def write_assistant_pack(
    *,
    log_dir: Path,
    config_path: Path,
    out_path: Optional[Path] = None,
    max_lines: int = DEFAULT_LINES,
) -> Path:
    out = build_assistant_pack(log_dir=log_dir, config_path=config_path, max_lines=max_lines)
    target = out_path or (log_dir / "assistant_pack.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Build assistant pack JSON")
    parser.add_argument("--log-dir", default="logs", help="logs directory")
    parser.add_argument("--config", default="bot_config.json", help="config path")
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES, help="lines per JSONL")
    parser.add_argument("--write", action="store_true", help="write to logs/assistant_pack.json")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    config_path = Path(args.config)
    if args.write:
        path = write_assistant_pack(log_dir=log_dir, config_path=config_path, max_lines=int(args.lines))
        print(str(path))
    else:
        pack = build_assistant_pack(log_dir=log_dir, config_path=config_path, max_lines=int(args.lines))
        print(json.dumps(pack))


if __name__ == "__main__":
    main()
