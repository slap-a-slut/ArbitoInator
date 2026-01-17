# infra/rpc.py

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Optional, Sequence, List

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

    async def call(self, method: str, params: list, *, timeout_s: Optional[float] = None) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params,
        }

        last_err: Optional[str] = None
        session = await self._get_session()
        to_s = float(timeout_s) if timeout_s is not None else self.default_timeout_s

        for attempt in range(self.max_retries + 1):
            try:
                async def _do():
                    async with session.post(self.url, json=payload) as resp:
                        # hard-fail on HTTP status; we'll classify below
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

                if isinstance(data, dict) and "error" in data:
                    # Many Ethereum nodes return revert data inside error.data for eth_call.
                    # Uniswap Quoter / QuoterV2 intentionally revert to surface quote results.
                    err = data["error"]
                    err_data = err.get("data") if isinstance(err, dict) else None

                    # Geth-style: { data: { <txHash>: { error, return } } }
                    if isinstance(err_data, dict):
                        if "data" in err_data and isinstance(err_data["data"], str):
                            return err_data["data"]
                        if "result" in err_data and isinstance(err_data["result"], str):
                            return err_data["result"]
                        for _k, v in err_data.items():
                            if isinstance(v, dict):
                                if isinstance(v.get("return"), str):
                                    return v["return"]
                                if isinstance(v.get("data"), str):
                                    return v["data"]
                    if isinstance(err_data, str):
                        return err_data

                    # Real error
                    last_err = f"rpc_error:{err}"
                    raise RuntimeError(last_err)

                return data["result"]

            except asyncio.TimeoutError:
                last_err = f"timeout({to_s}s)"
            except aiohttp.ClientResponseError as e:
                # Rate limit / server errors should retry
                last_err = f"http_{e.status}"
                if e.status not in (429, 500, 502, 503, 504):
                    break
            except (aiohttp.ClientError, RuntimeError, ValueError) as e:
                last_err = f"{type(e).__name__}: {e}"

            if attempt < self.max_retries:
                # exponential backoff with jitter
                sleep_s = (self.backoff_base_s * (2 ** attempt)) + random.random() * 0.25
                await asyncio.sleep(sleep_s)

        raise Exception(f"RPC call failed after retries: {last_err}")

    async def eth_call(self, to: str, data: str, block: str = "latest", *, timeout_s: Optional[float] = None) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
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
    ):
        cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not cleaned:
            raise ValueError("RPCPool requires at least one url")

        self.urls: List[str] = cleaned
        self._clients = [AsyncRPC(u, default_timeout_s=default_timeout_s) for u in self.urls]

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

    async def _pick_idx(self, banned: Set[int]) -> int:
        async with self._lock:
            best_idx = None
            best_val = None
            for i, n in enumerate(self._inflight):
                if i in banned:
                    continue
                if best_val is None or n < best_val:
                    best_val = n
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
            })
        return out

    async def call(self, method: str, params: list, *, timeout_s: Optional[float] = None) -> Any:
        banned: Set[int] = set()
        attempts = 0
        last_err: Optional[Exception] = None

        # Total attempts = number of endpoints tried, plus optional extra retries
        max_total = len(self._clients) + self.max_retries_per_call

        while attempts < max_total:
            idx = await self._pick_idx(banned)
            c = self._clients[idx]
            try:
                res = await c.call(method, params, timeout_s=timeout_s)
                await self._done_idx(idx, ok=True)
                return res
            except Exception as e:
                last_err = e
                banned.add(idx)
                await self._done_idx(idx, ok=False)
                attempts += 1
                continue

        raise last_err or Exception("RPCPool call failed")

    async def eth_call(self, to: str, data: str, block: str = "latest", *, timeout_s: Optional[float] = None) -> str:
        return await self.call(
            "eth_call",
            [{"to": to, "data": data}, block],
            timeout_s=timeout_s,
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
