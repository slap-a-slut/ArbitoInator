# infra/rpc.py

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any, Optional, Sequence, List, Dict, Set

import aiohttp
from web3 import Web3

from bot import config


def _split_urls(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    # Accept comma or newline separated lists.
    parts: List[str] = []
    for chunk in str(raw).replace("\n", ",").split(","):
        u = str(chunk).strip()
        if u:
            parts.append(u)
    return parts


def get_rpc_urls() -> List[str]:
    """Return RPC URL candidates in priority order.

    Order:
      1) env RPC_URLS (comma/newline list)
      2) bot.config.RPC_URLS (if present)
      3) env RPC_URL
      4) bot.config.RPC_URL
    """

    urls: List[str] = []
    urls.extend(_split_urls(os.getenv("RPC_URLS")))
    urls.extend([str(u).strip() for u in (getattr(config, "RPC_URLS", []) or []) if str(u).strip()])

    single = os.getenv("RPC_URL")
    if single:
        urls.append(single.strip())

    if not urls:
        urls = [config.RPC_URL]

    # De-dupe while preserving order
    out: List[str] = []
    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


class AsyncRPC:
    """Async JSON-RPC client with:
    - persistent aiohttp session (fast)
    - per-call timeouts (prevents "first block never ends")
    - retries + exponential backoff for transient errors / rate limits
    - Uniswap Quoter revert-data extraction for eth_call
    """

    def __init__(
        self,
        url: str,
        *,
        default_timeout_s: float = 12.0,
        max_retries: int = 4,
        backoff_base_s: float = 0.35,
    ):
        self.url = url
        self.default_timeout_s = float(default_timeout_s)
        self.max_retries = int(max_retries)
        self.backoff_base_s = float(backoff_base_s)
        self._id = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None

        # Lightweight stats (useful even for single-RPC mode)
        self._stats_lock = asyncio.Lock()
        self._inflight = 0
        self._ok = 0
        self._fail = 0
        self._lat_ewma_ms = 350.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        # Limit total sockets to avoid exploding the public RPC.
        self._connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
        self._session = aiohttp.ClientSession(connector=self._connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        if self._connector:
            await self._connector.close()
        self._connector = None

    async def call(
        self,
        method: str,
        params: list,
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
    ) -> Any:
        """Perform a JSON-RPC call.

        IMPORTANT: Some contracts (e.g., Uniswap V3 Quoter v1) intentionally revert
        to return data for eth_call. That revert-data pass-through must be *opt-in*
        via allow_revert_data=True; otherwise, downstream ABI decoding can turn
        Solidity revert payloads into absurd huge numbers.
        """

        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}

        session = await self._get_session()
        to_s = float(timeout_s) if timeout_s is not None else self.default_timeout_s
        last_err: Optional[str] = None

        async with self._stats_lock:
            self._inflight += 1

        try:
            for attempt in range(self.max_retries + 1):
                t0 = time.perf_counter()
                try:
                    async def _do():
                        async with session.post(self.url, json=payload) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                raise aiohttp.ClientResponseError(
                                    request_info=resp.request_info,
                                    history=resp.history,
                                    status=resp.status,
                                    message=text,
                                    headers=resp.headers,
                                )
                            return await resp.json()

                    data = await asyncio.wait_for(_do(), timeout=to_s)

                    # EWMA latency update (success path and allowed-revert path)
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                    async with self._stats_lock:
                        self._lat_ewma_ms = 0.8 * self._lat_ewma_ms + 0.2 * float(dt_ms)

                    if isinstance(data, dict) and "error" in data:
                        err = data["error"]
                        err_data = err.get("data") if isinstance(err, dict) else None

                        def _extract_revert_hex(ed: Any) -> Optional[str]:
                            if isinstance(ed, dict):
                                if isinstance(ed.get("data"), str):
                                    return ed["data"]
                                if isinstance(ed.get("result"), str):
                                    return ed["result"]
                                for _k, v in ed.items():
                                    if isinstance(v, dict):
                                        if isinstance(v.get("return"), str):
                                            return v["return"]
                                        if isinstance(v.get("data"), str):
                                            return v["data"]
                            if isinstance(ed, str):
                                return ed
                            return None

                        revert_hex = _extract_revert_hex(err_data)
                        if revert_hex and allow_revert_data and method == "eth_call":
                            hx = revert_hex if revert_hex.startswith("0x") else ("0x" + revert_hex)
                            # Guard against common Solidity error selectors.
                            if hx.startswith("0x08c379a0") or hx.startswith("0x4e487b71"):
                                last_err = f"revert({hx[:10]})"
                                raise RuntimeError(last_err)
                            async with self._stats_lock:
                                self._ok += 1
                            return hx

                        last_err = f"rpc_error:{err}"
                        raise RuntimeError(last_err)

                    async with self._stats_lock:
                        self._ok += 1
                    return data["result"]

                except asyncio.TimeoutError:
                    last_err = f"timeout({to_s}s)"
                except aiohttp.ClientResponseError as e:
                    last_err = f"http_{e.status}"
                    if e.status not in (429, 500, 502, 503, 504):
                        break
                except (aiohttp.ClientError, RuntimeError, ValueError) as e:
                    last_err = f"{type(e).__name__}: {e}"

                if attempt < self.max_retries:
                    sleep_s = (self.backoff_base_s * (2 ** attempt)) + random.random() * 0.25
                    await asyncio.sleep(sleep_s)

            async with self._stats_lock:
                self._fail += 1
            raise Exception(f"RPC call failed after retries: {last_err}")

        finally:
            async with self._stats_lock:
                self._inflight = max(0, self._inflight - 1)

    def stats(self) -> List[Dict[str, Any]]:
        return [
            {
                "url": self.url,
                "inflight": self._inflight,
                "ok": self._ok,
                "fail": self._fail,
                "lat_ms": round(float(self._lat_ewma_ms), 1),
            }
        ]

    async def eth_call(
        self,
        to: str,
        data: str,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
    ) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
            allow_revert_data=allow_revert_data,
        )

    async def get_block_number(self, *, timeout_s: Optional[float] = None) -> int:
        res = await self.call("eth_blockNumber", [], timeout_s=timeout_s)
        return int(res, 16)


class RPCPool:
    """Parallel RPC pool (load-balancing across all RPCs).

    This is NOT a failover-only design. Each request is routed to a single RPC
    endpoint chosen by least in-flight requests (so concurrent quoting spreads
    over all endpoints). On error/timeout, it retries on another endpoint.

    This approach:
      - uses all RPCs in parallel (higher throughput)
      - reduces per-endpoint rate limits
      - still survives flaky endpoints via retry-on-others

    Note: We intentionally do *not* send the same request to all RPCs (hedging),
    because that multiplies request volume.
    """

    def __init__(
        self,
        urls: Sequence[str],
        *,
        default_timeout_s: float = 12.0,
        max_retries_per_call: int = 1,
        per_rpc_max_inflight: int = 10,
        ewma_alpha: float = 0.20,
    ):
        cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not cleaned:
            raise ValueError("RPCPool requires at least one url")

        self.urls: List[str] = cleaned
        self._clients = [AsyncRPC(u, default_timeout_s=default_timeout_s) for u in self.urls]

        # Per-endpoint concurrency guard (prevents a single RPC from being flooded)
        self._sems: List[asyncio.Semaphore] = [asyncio.Semaphore(max(1, int(per_rpc_max_inflight))) for _ in self._clients]

        # Latency EWMA per endpoint (ms). Start with a modest baseline so a new RPC isn't unfairly punished.
        self._lat_ewma_ms: List[float] = [350.0 for _ in self._clients]
        self._ewma_alpha = float(ewma_alpha)

        # In-flight counters for least-load selection
        self._inflight: List[int] = [0 for _ in self._clients]
        self._lock = asyncio.Lock()

        # Simple stats (useful for debugging)
        self._ok: List[int] = [0 for _ in self._clients]
        self._fail: List[int] = [0 for _ in self._clients]

        self.max_retries_per_call = int(max_retries_per_call)

    async def close(self) -> None:
        for c in self._clients:
            try:
                await c.close()
            except Exception:
                pass

    def _score(self, idx: int) -> float:
        """Lower is better."""
        ok = self._ok[idx]
        fail = self._fail[idx]
        fail_rate = float(fail) / float(ok + fail + 1)
        # inflight dominates, then latency, then error penalty
        return float(self._inflight[idx]) + (self._lat_ewma_ms[idx] / 250.0) + (fail_rate * 6.0)

    async def _pick_idx(self, banned: Set[int]) -> int:
        async with self._lock:
            best_idx: Optional[int] = None
            best_val: Optional[float] = None
            for i, _n in enumerate(self._inflight):
                if i in banned:
                    continue
                val = self._score(i)
                if best_val is None or val < best_val:
                    best_val = val
                    best_idx = i
            if best_idx is None:
                # All banned: pick a random one to avoid dead-end
                best_idx = random.randrange(len(self._clients))
            self._inflight[best_idx] += 1
            return best_idx

    async def _done_idx(self, idx: int, ok: bool) -> None:
        async with self._lock:
            self._inflight[idx] = max(0, self._inflight[idx] - 1)
            if ok:
                self._ok[idx] += 1
            else:
                self._fail[idx] += 1

    def stats(self) -> List[Dict[str, Any]]:
        """Return per-RPC counters."""
        out: List[Dict[str, Any]] = []
        for i, url in enumerate(self.urls):
            out.append({
                "url": url,
                "inflight": self._inflight[i],
                "ok": self._ok[i],
                "fail": self._fail[i],
                "lat_ms": round(float(self._lat_ewma_ms[i]), 1),
            })
        return out

    async def call(
        self,
        method: str,
        params: list,
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
    ) -> Any:
        banned: Set[int] = set()
        attempts = 0
        last_err: Optional[Exception] = None

        # Total attempts = number of endpoints tried, plus optional extra retries
        max_total = len(self._clients) + self.max_retries_per_call

        while attempts < max_total:
            idx = await self._pick_idx(banned)
            c = self._clients[idx]
            try:
                # Per-endpoint concurrency guard + latency tracking
                async with self._sems[idx]:
                    t0 = time.perf_counter()
                    res = await c.call(method, params, timeout_s=timeout_s, allow_revert_data=allow_revert_data)
                    dt_ms = (time.perf_counter() - t0) * 1000.0
                # EWMA update
                self._lat_ewma_ms[idx] = (1.0 - self._ewma_alpha) * self._lat_ewma_ms[idx] + self._ewma_alpha * float(dt_ms)
                await self._done_idx(idx, ok=True)
                return res
            except Exception as e:
                last_err = e
                banned.add(idx)
                await self._done_idx(idx, ok=False)
                attempts += 1
                continue

        raise last_err or Exception("RPCPool call failed")

    async def eth_call(
        self,
        to: str,
        data: str,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
        allow_revert_data: bool = False,
    ) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
            allow_revert_data=allow_revert_data,
        )

    async def get_block_number(self, *, timeout_s: Optional[float] = None) -> int:
        res = await self.call("eth_blockNumber", [], timeout_s=timeout_s)
        return int(res, 16)


# Backwards-compat alias (older code imports MultiAsyncRPC)
MultiAsyncRPC = RPCPool


# ------------------------
# Synchronous Web3 provider helper for fork_test.py

def get_provider(rpc_urls: Optional[Sequence[str]] = None) -> Web3:
    """Return a connected Web3 provider.

    Uses multiple URLs if provided (env RPC_URLS="a,b,c" or bot.config.RPC_URLS).
    Falls back to bot.config.RPC_URL.
    """

    urls = list(rpc_urls) if rpc_urls else get_rpc_urls()

    last_err: Optional[str] = None
    for url in urls:
        provider = Web3(Web3.HTTPProvider(url))
        try:
            if provider.is_connected():
                return provider
        except Exception as e:
            last_err = str(e)
            continue

    raise ConnectionError(f"Cannot connect to any RPC endpoint. Last error: {last_err}")
