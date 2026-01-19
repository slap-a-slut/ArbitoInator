from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets

from infra.rpc import RPCPool
from mempool.types import PendingTx


PendingTxCallback = Callable[[PendingTx], Awaitable[None]]
StatusCallback = Callable[[Dict[str, Any]], Awaitable[None]]


class MempoolListener:
    def __init__(
        self,
        rpc: RPCPool,
        ws_urls: List[str],
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
        self.max_inflight = int(max_inflight)
        self.fetch_concurrency = int(fetch_concurrency)
        self.dedup_ttl_s = int(dedup_ttl_s)
        self.status_cb = status_cb
        self.tx_cb = tx_cb
        self.rpc_timeout_s = float(rpc_timeout_s)

        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max(1, self.max_inflight))
        self._seen: Dict[str, float] = {}
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._fetch_tasks: List[asyncio.Task] = []
        self._status_task: Optional[asyncio.Task] = None

        self._ws_connected = False
        self._ws_reconnects = 0
        self._ws_url: Optional[str] = None
        self._hash_count = 0
        self._fetch_ok = 0
        self._fetch_fail = 0
        self._last_rate_t = time.time()
        self._last_hash_count = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._status_task = asyncio.create_task(self._status_loop())
        self._fetch_tasks = [asyncio.create_task(self._fetch_loop(i)) for i in range(self.fetch_concurrency)]

    async def stop(self) -> None:
        self._running = False
        tasks = [t for t in [self._ws_task, self._status_task] if t]
        tasks.extend(self._fetch_tasks)
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
        total_fetch = self._fetch_ok + self._fetch_fail
        fetch_rate = (self._fetch_ok / total_fetch) if total_fetch > 0 else 0.0
        status = {
            "ws_connected": bool(self._ws_connected),
            "ws_url": self._ws_url,
            "ws_reconnects": int(self._ws_reconnects),
            "tx_hash_rate": float(hash_rate),
            "tx_fetch_success_rate": float(fetch_rate),
            "queue_size": int(self._queue.qsize()),
            "seen_cache_size": int(len(self._seen)),
            "ts": int(now * 1000),
        }
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

    async def _fetch_loop(self, idx: int) -> None:
        while self._running:
            try:
                tx_hash = await self._queue.get()
            except asyncio.CancelledError:
                break
            if not tx_hash:
                continue
            try:
                tx = await self._fetch_tx(tx_hash)
                if tx and self.tx_cb:
                    await self.tx_cb(tx)
            except Exception:
                continue

    async def _fetch_tx(self, tx_hash: str) -> Optional[PendingTx]:
        tx = None
        try:
            tx = await self.rpc.call("eth_getTransactionByHash", [tx_hash], timeout_s=self.rpc_timeout_s)
        except Exception:
            self._fetch_fail += 1
            return None
        if not tx:
            self._fetch_fail += 1
            return None

        def _to_int(value: Any) -> Optional[int]:
            if value is None:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                try:
                    return int(value, 16)
                except Exception:
                    return None
            return None

        try:
            pending = PendingTx(
                tx_hash=str(tx.get("hash") or tx_hash),
                from_addr=str(tx.get("from") or ""),
                to_addr=str(tx.get("to") or "") or None,
                input=str(tx.get("input") or ""),
                value=int(_to_int(tx.get("value")) or 0),
                max_fee_per_gas=_to_int(tx.get("maxFeePerGas")),
                max_priority_fee_per_gas=_to_int(tx.get("maxPriorityFeePerGas")),
                tx_type=_to_int(tx.get("type")),
                nonce=_to_int(tx.get("nonce")),
                seen_at_ms=int(time.time() * 1000),
            )
        except Exception:
            self._fetch_fail += 1
            return None

        self._fetch_ok += 1
        return pending

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
        if self._queue.full():
            return
        try:
            self._queue.put_nowait(h)
        except Exception:
            return
