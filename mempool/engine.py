from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from mempool.confirm import check_tx_receipt
from mempool.decoders.registry import build_decoders, build_router_registry, decode_pending_tx, is_known_selector
from mempool.listener import MempoolListener
from mempool.triggers import build_trigger
from mempool.types import DecodedSwap, PendingTx, Trigger


ScanFn = Callable[[Trigger, float], Awaitable[Dict[str, Any]]]


@dataclass
class TriggerRecord:
    trigger: Trigger
    pre_result: Optional[Dict[str, Any]]
    created_at_ms: int
    pre_block: Optional[int]
    post_block: Optional[int]
    post_best_net: Optional[float]


class MempoolEngine:
    def __init__(
        self,
        *,
        rpc,
        ws_urls: List[str],
        filter_to: List[str],
        univ2_routers: List[str],
        univ3_routers: List[str],
        universal_routers: List[str],
        min_value_usd: float,
        usd_per_eth: float,
        max_inflight: int,
        fetch_concurrency: int,
        dedup_ttl_s: int,
        trigger_budget_s: float,
        trigger_queue_max: int,
        trigger_concurrency: int,
        trigger_ttl_s: int,
        confirm_timeout_s: float,
        post_scan_budget_s: float,
        log_dir: Path,
        ui_push: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ) -> None:
        self.rpc = rpc
        self.ws_urls = ws_urls
        self.filter_to = {str(x).lower() for x in filter_to if str(x).strip()}
        self.min_value_usd = float(min_value_usd)
        self.usd_per_eth = float(usd_per_eth)
        self.trigger_budget_s = float(trigger_budget_s)
        self.trigger_queue_max = int(trigger_queue_max)
        self.trigger_concurrency = int(max(1, trigger_concurrency))
        self.trigger_ttl_s = int(trigger_ttl_s)
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.post_scan_budget_s = float(post_scan_budget_s)
        self.ui_push = ui_push

        router_registry = build_router_registry(
            univ2_routers=univ2_routers,
            univ3_routers=univ3_routers,
            universal_routers=universal_routers,
        )
        self.decoders = build_decoders(router_registry)

        self.log_dir = log_dir
        self.mempool_log = log_dir / "mempool.jsonl"
        self.trigger_log = log_dir / "trigger_scans.jsonl"
        self.status_path = log_dir / "mempool_status.json"
        self.recent_path = log_dir / "mempool_recent.json"
        self.triggers_path = log_dir / "mempool_triggers.json"

        self._listener = MempoolListener(
            rpc,
            ws_urls,
            max_inflight=max_inflight,
            fetch_concurrency=fetch_concurrency,
            dedup_ttl_s=dedup_ttl_s,
            status_cb=self._on_status,
            tx_cb=self._on_pending_tx,
            rpc_timeout_s=confirm_timeout_s,
        )

        self._scan_fn: Optional[ScanFn] = None
        self._queue: asyncio.Queue[Trigger] = asyncio.Queue(maxsize=max(1, self.trigger_queue_max))
        self._trigger_tasks: List[asyncio.Task] = []
        self._running = False
        self._current_block: Optional[int] = None
        self._recent_swaps: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._recent_triggers: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._pending: Dict[str, TriggerRecord] = {}
        self._running_triggers = 0

    async def start(self, scan_fn: ScanFn) -> None:
        if self._running:
            return
        self._scan_fn = scan_fn
        self._running = True
        await self._listener.start()
        self._trigger_tasks = [asyncio.create_task(self._trigger_worker()) for _ in range(self.trigger_concurrency)]

    async def stop(self) -> None:
        self._running = False
        await self._listener.stop()
        for t in self._trigger_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._trigger_tasks, return_exceptions=True)

    def set_block_number(self, block_number: int) -> None:
        self._current_block = int(block_number)

    async def confirm_block(self, block_number: int) -> None:
        now_ms = int(time.time() * 1000)
        stale = []
        for tx_hash, record in self._pending.items():
            if (now_ms - record.created_at_ms) > (self.trigger_ttl_s * 1000):
                stale.append(tx_hash)
        for tx_hash in stale:
            self._pending.pop(tx_hash, None)

        pending_hashes = list(self._pending.keys())
        if not pending_hashes:
            return

        for tx_hash in pending_hashes[:50]:
            included, included_block = await check_tx_receipt(self.rpc, tx_hash, timeout_s=self.confirm_timeout_s)
            if not included:
                continue
            record = self._pending.pop(tx_hash, None)
            if not record:
                continue
            record.post_block = included_block
            if self._scan_fn:
                post = await self._scan_fn(record.trigger, float(self.post_scan_budget_s))
                record.post_best_net = float(post.get("best_net", 0.0)) if post else 0.0
                self._append_trigger_result(record, post_result=post)

    async def _on_status(self, status: Dict[str, Any]) -> None:
        payload = dict(status)
        payload["triggers_queued"] = int(self._queue.qsize())
        payload["triggers_running"] = int(self._running_triggers)
        payload["current_block"] = int(self._current_block or 0)
        self._write_json(self.status_path, payload)
        if self.ui_push:
            await self.ui_push({"type": "mempool_status", **payload})

    async def _on_pending_tx(self, tx: PendingTx) -> None:
        to_addr = (tx.to_addr or "").lower()
        if self.filter_to and to_addr not in self.filter_to and not is_known_selector(tx.input):
            await self._log_mempool(tx, status="ignored", reason="not_watched")
            return

        decoded, reason = decode_pending_tx(self.decoders, tx)
        if not decoded:
            await self._log_mempool(tx, status="failed", reason=reason or "decode_failed")
            return

        trigger, reason = build_trigger(decoded, min_value_usd=self.min_value_usd, usd_per_eth=self.usd_per_eth)
        summary = self._decoded_summary(decoded)
        self._recent_swaps.append(summary)
        self._write_json(self.recent_path, list(self._recent_swaps))
        if self.ui_push:
            await self.ui_push({"type": "mempool_swap", **summary})
        if not trigger:
            await self._log_mempool(tx, status="ignored", reason=reason or "trigger_filtered", decoded_summary=summary)
            return

        if self._queue.full():
            await self._log_mempool(tx, status="ignored", reason="trigger_queue_full", decoded_summary=summary)
            return

        try:
            self._queue.put_nowait(trigger)
        except Exception:
            await self._log_mempool(tx, status="ignored", reason="trigger_enqueue_failed", decoded_summary=summary)
            return

        await self._log_mempool(tx, status="decoded", reason=None, decoded_summary=summary)

    async def _trigger_worker(self) -> None:
        while self._running:
            try:
                trigger = await self._queue.get()
            except asyncio.CancelledError:
                break
            if not trigger:
                continue
            now_ms = int(time.time() * 1000)
            if now_ms - trigger.created_at_ms > int(self.trigger_ttl_s * 1000):
                continue
            if not self._scan_fn:
                continue
            self._running_triggers += 1
            try:
                result = await self._scan_fn(trigger, float(self.trigger_budget_s))
                record = TriggerRecord(
                    trigger=trigger,
                    pre_result=result,
                    created_at_ms=trigger.created_at_ms,
                    pre_block=int(self._current_block) if self._current_block else None,
                    post_block=None,
                    post_best_net=None,
                )
                self._pending[trigger.tx_hash] = record
                self._append_trigger_result(record, post_result=None)
            except Exception:
                continue
            finally:
                self._running_triggers = max(0, self._running_triggers - 1)

    def _decoded_summary(self, decoded: DecodedSwap) -> Dict[str, Any]:
        return {
            "hash": decoded.tx_hash,
            "kind": decoded.kind,
            "router": decoded.router,
            "token_in": decoded.token_in,
            "token_out": decoded.token_out,
            "amount_in": decoded.amount_in,
            "amount_out_min": decoded.amount_out_min,
            "path": decoded.path,
            "fee_tiers": decoded.fee_tiers,
            "seen_at_ms": decoded.seen_at_ms,
        }

    def _append_trigger_result(self, record: TriggerRecord, post_result: Optional[Dict[str, Any]]) -> None:
        pre = record.pre_result or {}
        entry = {
            "trigger_id": record.trigger.trigger_id,
            "tx_hash": record.trigger.tx_hash,
            "scheduled": int(pre.get("scheduled", 0)),
            "finished": int(pre.get("finished", 0)),
            "timeouts": int(pre.get("timeouts", 0)),
            "best_gross": float(pre.get("best_gross", 0.0)),
            "best_net": float(pre.get("best_net", 0.0)),
            "best_route": pre.get("best_route_summary"),
            "outcome": pre.get("outcome", "no_hit"),
            "ts": int(time.time() * 1000),
            "pre_block": record.pre_block,
            "post_block": record.post_block,
            "post_best_net": record.post_best_net,
        }
        self._recent_triggers.append(entry)
        self._write_json(self.triggers_path, list(self._recent_triggers))
        if post_result:
            entry["post_best_net"] = float(post_result.get("best_net", 0.0))
        try:
            self.trigger_log.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
        except Exception:
            pass
        if self.ui_push:
            asyncio.create_task(self.ui_push({"type": "trigger_scan", **entry}))

    async def _log_mempool(
        self,
        tx: PendingTx,
        *,
        status: str,
        reason: Optional[str],
        decoded_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "ts": int(time.time() * 1000),
            "hash": tx.tx_hash,
            "to": tx.to_addr,
            "status": status,
            "reason": reason,
            "decoded_summary": decoded_summary,
        }
        try:
            self.mempool_log.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def _write_json(self, path: Path, payload: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass
