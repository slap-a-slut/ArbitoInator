from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from eth_abi import encode
from eth_utils import keccak

from bot import config
from infra.metrics import METRICS


ZERO_ADDR = "0x0000000000000000000000000000000000000000"


def _selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


_SEL_GET_PAIR = _selector("getPair(address,address)")
_SEL_GET_POOL = _selector("getPool(address,address,uint24)")


def _norm_addr(addr: str) -> str:
    try:
        return str(config.token_address(addr)).lower()
    except Exception:
        return str(addr or "").lower()


def _sorted_tokens(token_a: str, token_b: str) -> Tuple[str, str]:
    a = _norm_addr(token_a)
    b = _norm_addr(token_b)
    if a <= b:
        return a, b
    return b, a


def _decode_address(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    hx = str(raw)
    if hx.startswith("0x"):
        hx = hx[2:]
    if len(hx) < 40:
        return None
    addr = "0x" + hx[-40:]
    addr = addr.lower()
    return addr


def _encode_get_pair(token0: str, token1: str) -> str:
    params = encode(["address", "address"], [token0, token1])
    return "0x" + _SEL_GET_PAIR + params.hex()


def _encode_get_pool(token0: str, token1: str, fee: int) -> str:
    params = encode(["address", "address", "uint24"], [token0, token1, int(fee)])
    return "0x" + _SEL_GET_POOL + params.hex()


@dataclass
class PoolKey:
    kind: str
    factory_id: str
    token0: str
    token1: str
    fee: Optional[int] = None

    def as_cache_key(self) -> Tuple[str, str, str, str, Optional[int]]:
        return (self.kind, self.factory_id, self.token0, self.token1, self.fee)

    def as_label(self) -> str:
        if self.kind == "v2":
            return f"v2:{self.factory_id}:{self.token0}/{self.token1}"
        fee = "" if self.fee is None else str(self.fee)
        return f"v3:{self.factory_id}:{self.token0}/{self.token1}:{fee}"


class PoolDiscovery:
    def __init__(
        self,
        rpc: Any,
        *,
        ttl_blocks: Optional[int] = None,
    ) -> None:
        self.rpc = rpc
        self.ttl_blocks = int(ttl_blocks if ttl_blocks is not None else getattr(config, "POOL_DISCOVERY_TTL_BLOCKS", 300))
        self._block_number: Optional[int] = None
        self._block_cache: Dict[Tuple[str, str, str, str, Optional[int]], str] = {}
        self._ttl_cache: Dict[Tuple[str, str, str, str, Optional[int]], Tuple[str, int, float]] = {}
        self._last_prune = 0.0

    def _reset_block(self, block_number: int) -> None:
        if self._block_number == block_number:
            return
        self._block_number = int(block_number)
        self._block_cache.clear()

    def _cache_get(self, key: PoolKey, block_number: int) -> Optional[str]:
        cache_key = key.as_cache_key()
        if cache_key in self._block_cache:
            METRICS.inc("pool_discovery_cache_hits_block", 1)
            val = self._block_cache[cache_key]
            if val == ZERO_ADDR:
                METRICS.inc("pool_discovery_negative_cache_hits", 1)
            return val

        entry = self._ttl_cache.get(cache_key)
        if entry:
            val, seen_block, _ts = entry
            if int(block_number) - int(seen_block) <= int(self.ttl_blocks):
                METRICS.inc("pool_discovery_cache_hits_ttl", 1)
                if val == ZERO_ADDR:
                    METRICS.inc("pool_discovery_negative_cache_hits", 1)
                self._block_cache[cache_key] = val
                return val
            else:
                self._ttl_cache.pop(cache_key, None)

        METRICS.inc("pool_discovery_cache_misses", 1)
        return None

    def _cache_set(self, key: PoolKey, value: str, block_number: int) -> None:
        cache_key = key.as_cache_key()
        self._block_cache[cache_key] = value
        self._ttl_cache[cache_key] = (value, int(block_number), time.time())
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 60:
            return
        self._last_prune = now
        if len(self._ttl_cache) <= 50_000:
            return
        cutoff = int(self.ttl_blocks)
        to_drop = [k for k, (_v, blk, _ts) in self._ttl_cache.items() if int(self._block_number or 0) - int(blk) > cutoff]
        for k in to_drop[:10_000]:
            self._ttl_cache.pop(k, None)

    async def discover(
        self,
        *,
        v2_pairs: List[Tuple[str, str, str, str]],
        v3_pools: List[Tuple[str, str, str, str, int]],
        block_ctx: Any,
        timeout_s: float,
    ) -> Dict[Tuple[str, str, str, str, Optional[int]], Optional[str]]:
        block_number = int(getattr(block_ctx, "block_number", 0) or 0)
        self._reset_block(block_number)
        results: Dict[Tuple[str, str, str, str, Optional[int]], Optional[str]] = {}

        v2_fetch: List[Tuple[PoolKey, str]] = []
        for factory_id, factory_addr, token_a, token_b in v2_pairs:
            token0, token1 = _sorted_tokens(token_a, token_b)
            key = PoolKey("v2", str(factory_id), token0, token1, None)
            cached = self._cache_get(key, block_number)
            results[key.as_cache_key()] = cached
            if cached is None:
                v2_fetch.append((key, str(factory_addr)))

        v3_fetch: List[Tuple[PoolKey, str]] = []
        for factory_id, factory_addr, token_a, token_b, fee in v3_pools:
            token0, token1 = _sorted_tokens(token_a, token_b)
            key = PoolKey("v3", str(factory_id), token0, token1, int(fee))
            cached = self._cache_get(key, block_number)
            results[key.as_cache_key()] = cached
            if cached is None:
                v3_fetch.append((key, str(factory_addr)))

        METRICS.inc("pool_discovery_requests_total", len(v2_fetch) + len(v3_fetch))

        if v2_fetch:
            params_list: List[list] = []
            for key, factory_addr in v2_fetch:
                data = _encode_get_pair(key.token0, key.token1)
                params_list.append([{"to": factory_addr, "data": data}, getattr(block_ctx, "block_tag", "latest")])
            try:
                batch = await self.rpc.call_batch("eth_call", params_list, timeout_s=timeout_s, block_ctx=block_ctx)
            except Exception:
                batch = [{"error": "batch_failed"} for _ in params_list]
            for (key, _factory_addr), entry in zip(v2_fetch, batch):
                val = entry.get("result") if isinstance(entry, dict) else None
                addr = _decode_address(val)
                if addr is None:
                    results[key.as_cache_key()] = None
                    continue
                if addr == ZERO_ADDR:
                    self._cache_set(key, ZERO_ADDR, block_number)
                    METRICS.inc_reason("pool_missing_keys", key.as_label(), 1)
                else:
                    self._cache_set(key, addr, block_number)
                results[key.as_cache_key()] = addr

        if v3_fetch:
            params_list = []
            for key, factory_addr in v3_fetch:
                data = _encode_get_pool(key.token0, key.token1, int(key.fee or 0))
                params_list.append([{"to": factory_addr, "data": data}, getattr(block_ctx, "block_tag", "latest")])
            try:
                batch = await self.rpc.call_batch("eth_call", params_list, timeout_s=timeout_s, block_ctx=block_ctx)
            except Exception:
                batch = [{"error": "batch_failed"} for _ in params_list]
            for (key, _factory_addr), entry in zip(v3_fetch, batch):
                val = entry.get("result") if isinstance(entry, dict) else None
                addr = _decode_address(val)
                if addr is None:
                    results[key.as_cache_key()] = None
                    continue
                if addr == ZERO_ADDR:
                    self._cache_set(key, ZERO_ADDR, block_number)
                    METRICS.inc_reason("pool_missing_keys", key.as_label(), 1)
                else:
                    self._cache_set(key, addr, block_number)
                results[key.as_cache_key()] = addr

        return results

