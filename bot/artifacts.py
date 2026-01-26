from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


def init_run_dir(base_dir: Path, meta: Optional[Dict[str, Any]] = None) -> Path:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    short = str(os.getpid())
    run_dir = base / f"{ts}_{short}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if meta:
        write_meta(run_dir, meta)
    return run_dir


def configure_logging(run_dir: Path) -> logging.Logger:
    log_dir = Path(run_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "run.log"

    logger = logging.getLogger("run_pack")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


def write_meta(run_dir: Path, meta: Dict[str, Any]) -> None:
    path = Path(run_dir) / "run_meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def append_jsonl(run_dir: Path, name: str, obj: Dict[str, Any]) -> None:
    path = Path(run_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.open("a", encoding="utf-8").write(json.dumps(obj) + "\n")


def write_snapshot(run_dir: Path, snapshot_dict: Dict[str, Any]) -> None:
    path = Path(run_dir) / "diagnostic_snapshot.json"
    path.write_text(json.dumps(snapshot_dict, indent=2), encoding="utf-8")

