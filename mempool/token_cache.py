from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from bot import config


DECIMALS_SELECTOR = "0x313ce567"
SYMBOL_SELECTOR = "0x95d89b41"


def _decode_int(hex_data: str) -> Optional[int]:
    if not hex_data or not isinstance(hex_data, str):
        return None
    hx = hex_data[2:] if hex_data.startswith("0x") else hex_data
    if len(hx) < 64:
        return None
    try:
        return int(hx[:64], 16)
    except Exception:
        return None


def _decode_symbol(hex_data: str) -> Optional[str]:
    if not hex_data or not isinstance(hex_data, str):
        return None
    hx = hex_data[2:] if hex_data.startswith("0x") else hex_data
    if len(hx) < 64:
        return None
    try:
        # Try dynamic string: offset(32) + length(32) + data
        offset = int(hx[:64], 16)
        if offset == 32 and len(hx) >= 128:
            strlen = int(hx[64:128], 16)
            data = hx[128:128 + strlen * 2]
            if data:
                return bytes.fromhex(data).decode("utf-8", errors="ignore").strip() or None
    except Exception:
        pass
    try:
        # Try bytes32 string
        raw = bytes.fromhex(hx[:64])
        return raw.rstrip(b"\x00").decode("utf-8", errors="ignore").strip() or None
    except Exception:
        return None


class TokenCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data = {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            return

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data), encoding="utf-8")
        except Exception:
            return

    def get(self, addr: str) -> Optional[Dict[str, Any]]:
        return self.data.get(str(addr or "").lower())

    def set(self, addr: str, symbol: str, decimals: int) -> None:
        a = str(addr or "").lower()
        if not a:
            return
        self.data[a] = {
            "symbol": str(symbol or ""),
            "decimals": int(decimals),
            "updated_at": int(time.time()),
        }
        self._write()

    def mark_failed(self, addr: str, reason: str) -> None:
        a = str(addr or "").lower()
        if not a:
            return
        entry = self.data.get(a, {})
        entry["failed_at"] = int(time.time())
        entry["fail_reason"] = str(reason)
        entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
        self.data[a] = entry
        self._write()

    def should_retry(self, addr: str) -> bool:
        a = str(addr or "").lower()
        entry = self.data.get(a)
        if not entry:
            return True
        failed_at = entry.get("failed_at")
        if not failed_at:
            return True
        retry_s = int(getattr(config, "TOKEN_METADATA_RETRY_S", 600))
        return (int(time.time()) - int(failed_at)) >= retry_s


async def resolve_token_metadata(
    rpc: Any,
    token: str,
    *,
    cache: TokenCache,
    timeout_s: float = 2.0,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    addr = str(token or "").lower()
    if not addr:
        return None, None, "invalid_token"
    cached = cache.get(addr)
    if cached and cached.get("decimals") is not None:
        return cached.get("symbol"), int(cached.get("decimals")), None
    if cached and not cache.should_retry(addr):
        return None, None, "unknown_metadata"

    try:
        dec_raw = await rpc.call("eth_call", [{"to": addr, "data": DECIMALS_SELECTOR}, "latest"], timeout_s=timeout_s)
        decimals = _decode_int(dec_raw)
        if decimals is None:
            cache.mark_failed(addr, "decimals_missing")
            config.mark_token_metadata_failed(addr)
            return None, None, "unknown_metadata"
    except Exception:
        cache.mark_failed(addr, "decimals_call_failed")
        config.mark_token_metadata_failed(addr)
        return None, None, "unknown_metadata"

    symbol = None
    try:
        sym_raw = await rpc.call("eth_call", [{"to": addr, "data": SYMBOL_SELECTOR}, "latest"], timeout_s=timeout_s)
        symbol = _decode_symbol(sym_raw)
    except Exception:
        symbol = None

    cache.set(addr, symbol or "", int(decimals))
    config.register_token_metadata(addr, symbol or "", int(decimals))
    return symbol, int(decimals), None
