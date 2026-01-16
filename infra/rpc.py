# infra/rpc.py

from __future__ import annotations

import asyncio
import os
import random
from typing import Any, Optional

import aiohttp
from web3 import Web3

from bot import config


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


# ------------------------
# Synchronous Web3 provider helper for fork_test.py

def get_provider() -> Web3:
    """Return a connected Web3 provider.

    Uses bot.config.RPC_URL by default; can be overridden by env var RPC_URL.
    """

    url = os.getenv("RPC_URL", config.RPC_URL)
    provider = Web3(Web3.HTTPProvider(url))
    if not provider.is_connected():
        raise ConnectionError(f"Cannot connect to RPC at {url}")
    return provider
