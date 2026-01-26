from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets

from infra.rpc import RPCPool
from mempool.types import PendingTx
from mempool.tx_fetcher import TxFetcher


PendingTxCallback = Callable[[PendingTx], Awaitable[None]]
StatusCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class MempoolListener:
    def __init__(
        self,
        rpc: RPCPool,
        ws_urls: List[str],
        http_urls: Optional[List[str]] = None,
        ws_http_pairing: Optional[Dict[str, str]] = None,
        *,
        max_inflight: int = 200,
        fetch_concurrency: int = 20,
        dedup_ttl_s: int = 120,
        status_cb: Optional[StatusCallback] = None,
        tx_cb: Optional[PendingTxCallback] = None,
        rpc_timeout_s: float = 3.0,
    ) -> None:
        self.rpc = rpc
        self.ws_urls = [str(u).strip() for u in ws_urls if str(u).strip()]
        if http_urls is None:
            http_urls = list(getattr(rpc, "urls", []) or [])
        self.http_urls = [str(u).strip() for u in http_urls if str(u).strip()]
        self.max_inflight = int(max_inflight)
        self.fetch_concurrency = int(fetch_concurrency)
        self.dedup_ttl_s = int(dedup_ttl_s)
        self.status_cb = status_cb
        self.tx_cb = tx_cb
        self.rpc_timeout_s = float(rpc_timeout_s)

        self._seen: Dict[str, float] = {}
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._status_task: Optional[asyncio.Task] = None

        self._ws_connected = False
        self._ws_reconnects = 0
        self._ws_url: Optional[str] = None
        self._hash_count = 0
        self._last_rate_t = time.time()
        self._last_hash_count = 0
        self._fetcher = TxFetcher(
            rpc,
            http_urls=self.http_urls,
            ws_urls=self.ws_urls,
            ws_http_pairing=ws_http_pairing,
            max_inflight=self.max_inflight,
            fetch_concurrency=self.fetch_concurrency,
            dedup_ttl_s=self.dedup_ttl_s,
            rpc_timeout_s=self.rpc_timeout_s,
            tx_cb=self.tx_cb,
        )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._fetcher.start()
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._status_task = asyncio.create_task(self._status_loop())

    async def stop(self) -> None:
        self._running = False
        await self._fetcher.stop()
        tasks = [t for t in [self._ws_task, self._status_task] if t]
        for t in tasks:
            if t and not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _prune_seen(self, now: float) -> None:
        ttl = float(self.dedup_ttl_s)
        if not self._seen:
            return
        for h, ts in list(self._seen.items()):
            if now - ts > ttl:
                self._seen.pop(h, None)

    async def _emit_status(self) -> None:
        if not self.status_cb:
            return
        now = time.time()
        dt = max(1e-6, now - self._last_rate_t)
        hash_rate = (self._hash_count - self._last_hash_count) / dt
        fetch_status = self._fetcher.status()
        status = {
            "ws_connected": bool(self._ws_connected),
            "ws_url": self._ws_url,
            "ws_reconnects": int(self._ws_reconnects),
            "tx_hash_rate": float(hash_rate),
            "tx_fetch_success_rate": float(fetch_status.get("tx_fetch_success_rate", 0.0)),
            "queue_size": int(fetch_status.get("tx_fetch_queue_depth", 0)),
            "seen_cache_size": int(len(self._seen)),
            "ts": int(now * 1000),
        }
        status.update(fetch_status)
        self._last_rate_t = now
        self._last_hash_count = self._hash_count
        await self.status_cb(status)

    async def _status_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(1.0)
                self._prune_seen(time.time())
                await self._emit_status()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    async def _ws_loop(self) -> None:
        if not self.ws_urls:
            return
        ws_idx = 0
        backoff_s = 1.0
        while self._running:
            url = self.ws_urls[ws_idx % len(self.ws_urls)]
            ws_idx += 1
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws_connected = True
                    self._ws_url = url
                    backoff_s = 1.0
                    sub_msg = {"id": 1, "jsonrpc": "2.0", "method": "eth_subscribe", "params": ["newPendingTransactions"]}
                    await ws.send(json.dumps(sub_msg))
                    while self._running:
                        raw = await ws.recv()
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        result = None
                        if isinstance(data, dict):
                            if data.get("method") == "eth_subscription":
                                params = data.get("params") or {}
                                result = params.get("result")
                            elif "result" in data and isinstance(data.get("result"), str):
                                result = data.get("result")
                        if isinstance(result, str) and result.startswith("0x"):
                            await self._handle_hash(result)
            except asyncio.CancelledError:
                break
            except Exception:
                self._ws_connected = False
                self._ws_reconnects += 1
                self._ws_url = url
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2.0, 30.0)
                continue

    async def _handle_hash(self, tx_hash: str) -> None:
        self._hash_count += 1
        now = time.time()
        h = tx_hash.lower()
        if h in self._seen and (now - self._seen[h]) < float(self.dedup_ttl_s):
            return
        self._seen[h] = now
        try:
            self._fetcher.enqueue(h, ws_url=self._ws_url)
        except Exception:
            return
