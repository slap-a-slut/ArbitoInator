from __future__ import annotations

from typing import Iterable, Optional

from bot import config


GET_PAIR_SELECTOR = "0xe6a43905"


def _norm_addr(addr: str) -> str:
    a = str(addr or "").lower()
    if a.startswith("0x"):
        return a[2:]
    return a


def _encode_get_pair(token0: str, token1: str) -> str:
    t0 = _norm_addr(token0).rjust(64, "0")
    t1 = _norm_addr(token1).rjust(64, "0")
    return GET_PAIR_SELECTOR + t0 + t1


def _is_zero_address(addr: str) -> bool:
    a = str(addr or "").lower()
    return a in ("", "0x", "0x0", "0x0000000000000000000000000000000000000000")


async def _has_v2_pair(
    rpc,
    factory: str,
    token_a: str,
    token_b: str,
    *,
    timeout_s: float,
) -> bool:
    data = _encode_get_pair(token_a, token_b)
    try:
        raw = await rpc.eth_call(factory, data, block="latest", timeout_s=timeout_s)
        if not raw:
            return False
        hx = raw[2:] if str(raw).startswith("0x") else str(raw)
        if len(hx) < 40:
            return False
        pair = "0x" + hx[-40:]
        return not _is_zero_address(pair)
    except Exception:
        return False


async def has_liquidity(
    rpc,
    token: str,
    connectors: Iterable[str],
    *,
    timeout_s: float = 2.0,
) -> bool:
    token_addr = str(token or "").lower()
    if not token_addr:
        return False
    factories = [
        getattr(config, "UNISWAP_V2_FACTORY", ""),
        getattr(config, "SUSHISWAP_FACTORY", ""),
    ]
    for base in connectors:
        base_addr = str(base or "").lower()
        if not base_addr or base_addr == token_addr:
            continue
        for factory in factories:
            if not factory:
                continue
            if await _has_v2_pair(rpc, factory, token_addr, base_addr, timeout_s=timeout_s):
                return True
    return False
