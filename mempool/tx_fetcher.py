from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from bot import config
from infra.metrics import METRICS
from infra.rpc import RPCPool, AsyncRPC
from mempool.types import PendingTx


@dataclass
class FetchItem:
    tx_hash: str
    attempts: int
    preferred_http: Optional[str]
    ws_source_id: Optional[str]
    received_at: float


TxCallback = Callable[[PendingTx], asyncio.Future]


def _normalize_error(err: Exception) -> str:
    msg = str(err).lower()
    if "timeout" in msg:
        return "timeout"
    if "rate limit" in msg or "http_429" in msg:
        return "rate_limited"
    if "http_5" in msg:
        return "http_5xx"
    if "decode" in msg:
        return "decode_error"
    if "rpc" in msg or "http_" in msg:
        return "rpc_error"
    return "internal_error"


def _host(u: str) -> str:
    raw = str(u or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return raw.split("/", 1)[0]


def _derive_http_url(ws_url: str) -> Optional[str]:
    raw = str(ws_url or "").strip()
    if raw.startswith("wss://"):
        return "https://" + raw[len("wss://"):]
    if raw.startswith("ws://"):
        return "http://" + raw[len("ws://"):]
    return None


def _ws_http_map(ws_urls: List[str], http_urls: List[str]) -> Dict[str, str]:

    http_by_host = {_host(u): u for u in http_urls if str(u).strip()}
    out: Dict[str, str] = {}
    for ws in ws_urls:
        h = _host(ws)
        if h in http_by_host:
            out[h] = http_by_host[h]
            continue
        derived = _derive_http_url(ws)
        if derived:
            out[h] = derived
    return out


class TxFetcher:
    def __init__(
        self,
        rpc: RPCPool,
        *,
        http_urls: List[str],
        ws_urls: List[str],
        ws_http_pairing: Optional[Dict[str, str]] = None,
        max_inflight: int = 200,
        fetch_concurrency: int = 20,
        dedup_ttl_s: int = 120,
        rpc_timeout_s: float = 3.0,
        batch_max: int = 50,
        tx_cb: Optional[Callable[[PendingTx], Any]] = None,
    ) -> None:
        self.rpc = rpc
        self.http_urls = [str(u).strip() for u in http_urls if str(u).strip()]
        self.ws_urls = [str(u).strip() for u in ws_urls if str(u).strip()]
        self.max_inflight = int(max_inflight)
        self.fetch_concurrency = int(fetch_concurrency)
        self.dedup_ttl_s = int(dedup_ttl_s)
        self.rpc_timeout_s = float(rpc_timeout_s)
        self.batch_max = max(1, int(batch_max))
        self.tx_cb = tx_cb

        self._queue: Optional[asyncio.PriorityQueue] = None
        self._pending: Dict[str, FetchItem] = {}
        self._seen: Dict[str, float] = {}
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._seq = 0
        self._map = _ws_http_map(self.ws_urls, self.http_urls)
        if isinstance(ws_http_pairing, dict):
            for ws_key, http_url in ws_http_pairing.items():
                if not ws_key or not http_url:
                    continue
                self._map[_host(ws_key)] = str(http_url).strip()
        self._pool_hosts = {_host(u) for u in getattr(rpc, "urls", []) or []}
        self._direct_clients: Dict[str, AsyncRPC] = {}
        self._direct_sems: Dict[str, asyncio.Semaphore] = {}
        self._direct_max_inflight = int(getattr(config, "TX_FETCH_PER_ENDPOINT_MAX_INFLIGHT", 5))
        self._batch_error_counts: Dict[str, int] = {}
        self._batch_disabled_until: Dict[str, float] = {}

        self._attempts_total = 0
        self._attempts_found = 0
        self._unique_total = 0
        self._unique_found = 0
        self._found_once: Dict[str, float] = {}
        self._fetch_not_found = 0
        self._fetch_timeout = 0
        self._fetch_rate_limited = 0
        self._fetch_errors_other = 0
        self._fetch_retries = 0
        self._batch_attempts = 0
        self._batch_errors = 0
        self._single_calls = 0

        retry_delays = getattr(config, "TX_FETCH_RETRY_BACKOFF_MS", None)
        if isinstance(retry_delays, (list, tuple)) and retry_delays:
            self._retry_delays_ms = [int(x) for x in retry_delays if int(x) >= 0]
        else:
            self._retry_delays_ms = [200, 500, 1000, 2000]
        max_retries = getattr(config, "TX_FETCH_MAX_RETRIES", None)
        if isinstance(max_retries, int) and max_retries > 0:
            self._retry_delays_ms = self._retry_delays_ms[: int(max_retries)]
        self._batch_disable_s = float(getattr(config, "TX_FETCH_BATCH_DISABLE_S", 300))
        self._batch_enabled = bool(getattr(config, "TX_FETCH_BATCH_ENABLED", True))

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._queue is None:
            self._queue = asyncio.PriorityQueue(maxsize=max(1, self.max_inflight))
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self.fetch_concurrency)]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for client in list(self._direct_clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._direct_clients.clear()
        self._direct_sems.clear()

    def enqueue(self, tx_hash: str, *, ws_url: Optional[str] = None) -> bool:
        now = time.time()
        h = str(tx_hash or "")
        if not h or not h.startswith("0x"):
            return False
        if self._queue is None:
            return False
        last = self._seen.get(h)
        if last and now - last < float(self.dedup_ttl_s):
            return False
        self._seen[h] = now
        self._prune_seen(now)
        if h in self._pending:
            return False
        preferred = None
        ws_source_id = None
        if ws_url:
            host = _host(ws_url)
            ws_source_id = host
            preferred = self._map.get(host)
        item = FetchItem(
            tx_hash=h,
            attempts=0,
            preferred_http=preferred,
            ws_source_id=ws_source_id,
            received_at=now,
        )
        self._pending[h] = item
        self._seq += 1
        try:
            self._queue.put_nowait((now, self._seq, h))
            self._note_unique_total(h)
            return True
        except asyncio.QueueFull:
            self._pending.pop(h, None)
            return False

    def status(self) -> Dict[str, Any]:
        attempts_total = int(self._attempts_total)
        attempts_found = int(self._attempts_found)
        unique_total = int(self._unique_total)
        unique_found = int(self._unique_found)
        attempt_rate = float(attempts_found) / float(attempts_total) if attempts_total > 0 else 0.0
        unique_rate = float(unique_found) / float(unique_total) if unique_total > 0 else 0.0
        now = time.time()
        disabled = [k for k, until in self._batch_disabled_until.items() if until and until > now]
        return {
            "tx_fetch_total": attempts_total,
            "tx_fetch_found": attempts_found,
            "tx_fetch_attempts_total": attempts_total,
            "tx_fetch_attempts_found": attempts_found,
            "tx_fetch_unique_total": unique_total,
            "tx_fetch_unique_found": unique_found,
            "tx_fetch_not_found": int(self._fetch_not_found),
            "tx_fetch_timeout": int(self._fetch_timeout),
            "tx_fetch_rate_limited": int(self._fetch_rate_limited),
            "tx_fetch_errors_other": int(self._fetch_errors_other),
            "tx_fetch_retries_total": int(self._fetch_retries),
            "tx_fetch_success_rate": float(unique_rate),
            "tx_fetch_attempt_success_rate": float(attempt_rate),
            "tx_fetch_unique_success_rate": float(unique_rate),
            "tx_fetch_queue_depth": int(self._queue.qsize()) if self._queue else 0,
            "tx_fetch_batch_attempts": int(self._batch_attempts),
            "tx_fetch_batch_errors": int(self._batch_errors),
            "tx_fetch_single_calls_total": int(self._single_calls),
            "tx_fetch_batch_disabled_endpoints_count": int(len(disabled)),
        }

    def _in_pool(self, url: Optional[str]) -> bool:
        if not url:
            return False
        return _host(url) in self._pool_hosts

    def _direct_client(self, url: str) -> AsyncRPC:
        client = self._direct_clients.get(url)
        if client:
            return client
        client = AsyncRPC(url, default_timeout_s=self.rpc_timeout_s, max_retries=0)
        self._direct_clients[url] = client
        self._direct_sems[url] = asyncio.Semaphore(max(1, int(self._direct_max_inflight)))
        return client

    def _prune_seen(self, now: float) -> None:
        ttl = float(self.dedup_ttl_s)
        for h, ts in list(self._seen.items()):
            if now - ts > ttl:
                self._seen.pop(h, None)
        for h, ts in list(self._found_once.items()):
            if now - ts > ttl:
                self._found_once.pop(h, None)

    def _note_attempts(self, count: int) -> None:
        if count <= 0:
            return
        self._attempts_total += int(count)
        METRICS.inc("tx_fetch_attempts_total", int(count))
        METRICS.inc("tx_fetch_total", int(count))

    def _note_attempt_found(self, count: int = 1) -> None:
        if count <= 0:
            return
        self._attempts_found += int(count)
        METRICS.inc("tx_fetch_attempts_found", int(count))
        METRICS.inc("tx_fetch_found", int(count))

    def _note_unique_total(self, tx_hash: str) -> None:
        if not tx_hash:
            return
        self._unique_total += 1
        METRICS.inc("tx_fetch_unique_total", 1)

    def _note_unique_found(self, tx_hash: str) -> None:
        if not tx_hash:
            return
        if tx_hash in self._found_once:
            return
        self._found_once[tx_hash] = time.time()
        self._unique_found += 1
        METRICS.inc("tx_fetch_unique_found", 1)

    async def _worker(self, idx: int) -> None:
        while self._running:
            try:
                if not self._queue:
                    await asyncio.sleep(0.01)
                    continue
                next_at, _, tx_hash = await self._queue.get()
            except asyncio.CancelledError:
                break
            now = time.time()
            if next_at > now:
                await asyncio.sleep(max(0.0, next_at - now))
            item = self._pending.get(tx_hash)
            if not item:
                continue
            preferred = item.preferred_http
            batch = [item]

            # Pull more items for the same preferred endpoint.
            while len(batch) < self.batch_max:
                try:
                    if not self._queue:
                        break
                    next_at2, _, h2 = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if next_at2 > time.time():
                    self._seq += 1
                    if self._queue:
                        self._queue.put_nowait((next_at2, self._seq, h2))
                    break
                it2 = self._pending.get(h2)
                if not it2 or it2.preferred_http != preferred:
                    self._seq += 1
                    if self._queue:
                        self._queue.put_nowait((time.time(), self._seq, h2))
                    continue
                batch.append(it2)

            await self._fetch_batch(batch, preferred_http=preferred)

    async def _fetch_batch(self, batch: List[FetchItem], *, preferred_http: Optional[str]) -> None:
        if not batch:
            return
        if not self._batch_enabled and len(batch) > 1:
            if preferred_http and not self._in_pool(preferred_http):
                await self._fetch_individual_direct(batch, preferred_http=preferred_http)
            else:
                await self._fetch_individual(batch, preferred_http=preferred_http)
            return
        if preferred_http and not self._in_pool(preferred_http):
            await self._fetch_individual_direct(batch, preferred_http=preferred_http)
            return
        hashes = [b.tx_hash for b in batch]
        params = [[h] for h in hashes]
        METRICS.observe("tx_fetch_batch_size", float(len(hashes)))
        if len(hashes) > 1:
            self._batch_attempts += 1
            METRICS.inc("tx_fetch_batch_attempts", 1)

        block_ctx = None
        if preferred_http:
            block_ctx = type("Ctx", (), {"pinned_http_endpoint": preferred_http})

        if preferred_http and len(batch) > 1:
            disabled_until = self._batch_disabled_until.get(preferred_http)
            if disabled_until and disabled_until > time.time():
                await self._fetch_individual(batch, preferred_http=preferred_http)
                return

        self._note_attempts(len(hashes))
        try:
            responses = await self.rpc.call_batch(
                "eth_getTransactionByHash",
                params,
                timeout_s=self.rpc_timeout_s,
                block_ctx=block_ctx,
            )
        except Exception as exc:
            if len(hashes) > 1:
                self._batch_errors += 1
                METRICS.inc("tx_fetch_batch_errors", 1)
            if len(batch) > 1:
                if preferred_http:
                    count = int(self._batch_error_counts.get(preferred_http, 0)) + 1
                    self._batch_error_counts[preferred_http] = count
                    if count >= 2:
                        self._batch_disabled_until[preferred_http] = time.time() + float(self._batch_disable_s)
                await self._fetch_individual(batch, preferred_http=preferred_http)
                return
            reason = _normalize_error(exc)
            await self._retry_or_drop(batch, reason=reason if reason else "rpc_error")
            return

        if preferred_http:
            self._batch_error_counts[preferred_http] = 0

        for idx, item in enumerate(batch):
            resp = responses[idx] if idx < len(responses) else {"error": "missing"}
            result = resp.get("result") if isinstance(resp, dict) else None
            err = resp.get("error") if isinstance(resp, dict) else None
            if err is not None:
                reason = "rpc_error"
                if isinstance(err, dict):
                    msg = str(err.get("message", "")).lower()
                    if "timeout" in msg:
                        reason = "timeout"
                    elif "rate" in msg or "limit" in msg:
                        reason = "rate_limited"
                await self._retry_or_drop([item], reason=reason)
                continue
            if not result:
                await self._retry_or_drop([item], reason="not_found")
                continue
            pending = self._to_pending(result)
            if not pending:
                await self._retry_or_drop([item], reason="decode_error")
                continue
            self._note_attempt_found(1)
            self._note_unique_found(item.tx_hash)
            self._pending.pop(item.tx_hash, None)
            if self.tx_cb:
                await self.tx_cb(pending)

    async def _fetch_individual(self, batch: List[FetchItem], *, preferred_http: Optional[str]) -> None:
        block_ctx = None
        if preferred_http:
            block_ctx = type("Ctx", (), {"pinned_http_endpoint": preferred_http})
        for item in batch:
            ok = await self._fetch_one_pool(item, block_ctx=block_ctx)
            if ok:
                continue

    async def _fetch_individual_direct(self, batch: List[FetchItem], *, preferred_http: str) -> None:
        client = self._direct_client(preferred_http)
        sem = self._direct_sems.get(preferred_http)
        for item in batch:
            self._single_calls += 1
            METRICS.inc("tx_fetch_single_calls_total", 1)
            self._note_attempts(1)
            try:
                if sem:
                    async with sem:
                        tx = await client.call(
                            "eth_getTransactionByHash",
                            [item.tx_hash],
                            timeout_s=self.rpc_timeout_s,
                        )
                else:
                    tx = await client.call(
                        "eth_getTransactionByHash",
                        [item.tx_hash],
                        timeout_s=self.rpc_timeout_s,
                    )
            except Exception as exc:
                reason = _normalize_error(exc)
                if reason in ("timeout", "rate_limited", "rpc_error"):
                    ok = await self._fetch_one_pool(item, block_ctx=None)
                    if ok:
                        continue
                await self._retry_or_drop([item], reason=reason if reason else "rpc_error")
                continue
            if not tx:
                await self._retry_or_drop([item], reason="not_found")
                continue
            pending = self._to_pending(tx)
            if not pending:
                await self._retry_or_drop([item], reason="decode_error")
                continue
            self._note_attempt_found(1)
            self._note_unique_found(item.tx_hash)
            self._pending.pop(item.tx_hash, None)
            if self.tx_cb:
                await self.tx_cb(pending)

    async def _fetch_one_pool(self, item: FetchItem, *, block_ctx: Optional[Any]) -> bool:
        self._single_calls += 1
        METRICS.inc("tx_fetch_single_calls_total", 1)
        self._note_attempts(1)
        try:
            tx = await self.rpc.call(
                "eth_getTransactionByHash",
                [item.tx_hash],
                timeout_s=self.rpc_timeout_s,
                block_ctx=block_ctx,
            )
        except Exception as exc:
            reason = _normalize_error(exc)
            await self._retry_or_drop([item], reason=reason if reason else "rpc_error")
            return False
        if not tx:
            await self._retry_or_drop([item], reason="not_found")
            return False
        pending = self._to_pending(tx)
        if not pending:
            await self._retry_or_drop([item], reason="decode_error")
            return False
        self._note_attempt_found(1)
        self._note_unique_found(item.tx_hash)
        self._pending.pop(item.tx_hash, None)
        if self.tx_cb:
            await self.tx_cb(pending)
        return True

    async def _retry_or_drop(self, items: List[FetchItem], *, reason: str) -> None:
        now = time.time()
        for item in items:
            item.attempts += 1
            if reason == "not_found":
                if item.attempts <= len(self._retry_delays_ms):
                    self._fetch_retries += 1
                    METRICS.inc("tx_fetch_retries_total", 1)
                    delay_ms = self._retry_delays_ms[item.attempts - 1]
                    self._seq += 1
                    if self._queue:
                        self._queue.put_nowait((now + (delay_ms / 1000.0), self._seq, item.tx_hash))
                    continue
                self._fetch_not_found += 1
                METRICS.inc("tx_fetch_not_found", 1)
                self._pending.pop(item.tx_hash, None)
                continue
            if reason == "timeout":
                self._fetch_timeout += 1
                METRICS.inc("tx_fetch_timeout", 1)
            elif reason == "rate_limited":
                self._fetch_rate_limited += 1
                METRICS.inc("tx_fetch_rate_limited", 1)
            else:
                self._fetch_errors_other += 1
                METRICS.inc("tx_fetch_errors_other", 1)
            if item.attempts <= len(self._retry_delays_ms):
                self._fetch_retries += 1
                METRICS.inc("tx_fetch_retries_total", 1)
                delay_ms = self._retry_delays_ms[min(item.attempts - 1, len(self._retry_delays_ms) - 1)]
                self._seq += 1
                if self._queue:
                    self._queue.put_nowait((now + (delay_ms / 1000.0), self._seq, item.tx_hash))
                continue
            self._pending.pop(item.tx_hash, None)

    def _to_pending(self, tx: Dict[str, Any]) -> Optional[PendingTx]:
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
                tx_hash=str(tx.get("hash") or ""),
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
            return None
        return pending
