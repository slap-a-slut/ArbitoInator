from __future__ import annotations

from typing import List, Tuple

from eth_utils import keccak


def selector(signature: str) -> str:
    return keccak(text=signature).hex()[:8]


def to_hex_prefixed(value: bytes) -> str:
    return "0x" + value.hex()


def normalize_address(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if value.startswith("0x"):
            return value.lower()
        return "0x" + value.lower()
    if isinstance(value, (bytes, bytearray)):
        return to_hex_prefixed(bytes(value)).lower()
    if hasattr(value, "hex"):
        try:
            return "0x" + value.hex().lower()
        except Exception:
            pass
    if isinstance(value, int):
        return "0x" + value.to_bytes(20, "big").hex()
    return str(value).lower()


def decode_v3_path(path_bytes: bytes) -> Tuple[List[str], List[int]]:
    tokens: List[str] = []
    fees: List[int] = []
    i = 0
    data = bytes(path_bytes)
    while i + 20 <= len(data):
        token = data[i : i + 20]
        tokens.append(to_hex_prefixed(token).lower())
        i += 20
        if i >= len(data):
            break
        if i + 3 > len(data):
            break
        fee = int.from_bytes(data[i : i + 3], "big")
        fees.append(fee)
        i += 3
    return tokens, fees
