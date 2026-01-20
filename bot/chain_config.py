from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ChainConfig:
    chain_id: Optional[int]
    name: str
    rpc_urls: list[str]
    tokens: Dict[str, Optional[str]]
    token_decimals: Dict[str, int]
    dex: Dict[str, str]


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_tokens(raw: Any) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not k:
            continue
        key = str(k).upper()
        if v is None:
            out[key] = None
            continue
        val = str(v).strip()
        out[key] = val if val else None
    return out


def _normalize_token_decimals(raw: Any) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not k:
            continue
        key = str(k).upper()
        try:
            out[key] = int(v)
        except Exception:
            continue
    return out


def _normalize_dex(raw: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        if not k:
            continue
        key = str(k).strip().lower()
        val = str(v).strip() if v is not None else ""
        out[key] = val
    return out


def load_chain_config(chain_name: Optional[str] = None, chain_id: Optional[int] = None) -> Optional[ChainConfig]:
    base_dir = Path(__file__).resolve().parents[1] / "configs" / "chains"
    name = str(chain_name or os.getenv("CHAIN_NAME") or "").strip().lower()
    chain_id_env = os.getenv("CHAIN_ID")
    cid = chain_id
    if cid is None and chain_id_env:
        try:
            cid = int(chain_id_env)
        except Exception:
            cid = None

    candidates: list[Path] = []
    if name:
        candidates.append(base_dir / f"{name}.json")
    if cid is not None:
        candidates.append(base_dir / f"{cid}.json")
    if not candidates:
        candidates.append(base_dir / "mainnet.json")

    data: Optional[Dict[str, Any]] = None
    for path in candidates:
        if path.exists():
            data = _read_json(path)
            if isinstance(data, dict):
                break
    if not data:
        return None

    chain_id_val = None
    try:
        if data.get("chain_id") is not None:
            chain_id_val = int(data.get("chain_id"))
    except Exception:
        chain_id_val = None

    return ChainConfig(
        chain_id=chain_id_val,
        name=str(data.get("name") or name or "unknown").strip().lower(),
        rpc_urls=[str(x).strip() for x in (data.get("rpc_urls") or []) if str(x).strip()],
        tokens=_normalize_tokens(data.get("tokens")),
        token_decimals=_normalize_token_decimals(data.get("token_decimals")),
        dex=_normalize_dex(data.get("dex")),
    )
