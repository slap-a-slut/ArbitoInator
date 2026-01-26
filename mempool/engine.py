from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from bot import config
from mempool.confirm import check_tx_receipt
from mempool.decoders.registry import build_decoders, decode_pending_tx, is_known_selector
from mempool.liquidity import has_liquidity
from mempool.listener import MempoolListener
from mempool.routers import build_router_registry, list_router_names
from mempool.token_cache import TokenCache, resolve_token_metadata
from mempool.triggers import build_trigger
from mempool.types import DecodedSwap, PendingTx, Trigger
from infra.metrics import METRICS


ScanFn = Callable[[Trigger, float], Awaitable[Dict[str, Any]]]

# Unified reason codes
REASON_NOT_WATCHED_TO = "not_watched_to"
REASON_IGNORED_SELECTOR = "ignored_selector"
REASON_DECODE_FAILED = "decode_failed"
REASON_TRIGGER_BELOW_USD = "trigger_below_threshold_usd"
REASON_TRIGGER_UNKNOWN_VALUE = "trigger_unknown_value"
REASON_TRIGGER_BELOW_RAW = "trigger_below_raw_threshold"
REASON_TRIGGER_MISSING_AMOUNT = "trigger_missing_amount"
REASON_TRIGGER_UNKNOWN_METADATA = "trigger_unknown_metadata"
REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY = "trigger_unknown_no_liquidity"
REASON_TRIGGER_UNKNOWN_NOT_IN_UNIVERSE = "trigger_unknown_not_in_universe"
REASON_TRIGGER_QUEUE_FULL = "trigger_queue_full"
REASON_TRIGGER_ENQUEUE_FAILED = "trigger_enqueue_failed"
REASON_TRIGGER_TTL_EXPIRED = "trigger_ttl_expired"


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
        http_urls: Optional[List[str]] = None,
        ws_http_pairing: Optional[Dict[str, str]] = None,
        filter_to: List[str],
        watch_mode: str,
        watched_router_set: str,
        min_value_usd: float,
        usd_per_eth: float,
        max_inflight: int,
        fetch_concurrency: int,
        dedup_ttl_s: int,
        trigger_budget_s: float,
        trigger_prepare_budget_ms: int,
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
        self.trigger_prepare_budget_ms = int(trigger_prepare_budget_ms)
        self.trigger_queue_max = int(trigger_queue_max)
        self.trigger_concurrency = int(max(1, trigger_concurrency))
        self.trigger_ttl_s = int(trigger_ttl_s)
        self.confirm_timeout_s = float(confirm_timeout_s)
        self.post_scan_budget_s = float(post_scan_budget_s)
        self.ui_push = ui_push

        self.watch_mode = str(watch_mode or "strict").strip().lower()
        if self.watch_mode not in ("strict", "routers_only"):
            self.watch_mode = "strict"
        self.router_set = str(watched_router_set or "core").strip().lower() or "core"
        self.router_registry = build_router_registry(
            router_set=self.router_set,
            extra_addresses=list(self.filter_to),
        )
        self._watched_router_names = list_router_names(self.router_registry)
        self.decoders = build_decoders(self.router_registry)
        self.decoders_any = build_decoders({})

        self.log_dir = log_dir
        self.mempool_log = log_dir / "mempool.jsonl"
        self.trigger_log = log_dir / "trigger_scans.jsonl"
        self.status_path = log_dir / "mempool_status.json"
        self.recent_path = log_dir / "mempool_recent.json"
        self.triggers_path = log_dir / "mempool_triggers.json"

        self._listener = MempoolListener(
            rpc,
            ws_urls,
            http_urls=http_urls or list(getattr(rpc, "urls", []) or []),
            ws_http_pairing=ws_http_pairing,
            max_inflight=max_inflight,
            fetch_concurrency=fetch_concurrency,
            dedup_ttl_s=dedup_ttl_s,
            status_cb=self._on_status,
            tx_cb=self._on_pending_tx,
            rpc_timeout_s=confirm_timeout_s,
        )

        self._scan_fn: Optional[ScanFn] = None
        self._queue: Optional[asyncio.Queue[Trigger]] = None
        self._trigger_tasks: List[asyncio.Task] = []
        self._running = False
        self._current_block: Optional[int] = None
        self._recent_swaps: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._recent_triggers: Deque[Dict[str, Any]] = deque(maxlen=20)
        self._pending: Dict[str, TriggerRecord] = {}
        self._running_triggers = 0
        self._triggers_seen = 0
        self._triggers_processed = 0
        self._triggers_dropped_ttl = 0
        self._triggers_dropped_queue = 0
        self._trigger_latency_sum_ms = 0.0
        self._trigger_latency_count = 0
        self._decoded_swaps_total = 0
        self._post_validations_total = 0
        self._persistent_hits = 0
        self._valid_hits = 0
        self._trigger_scans_scheduled = 0
        self._trigger_scans_finished = 0
        self._trigger_scans_timeouts = 0
        self._trigger_scans_zero_candidates = 0
        self._trigger_prepare_truncated = 0
        self._trigger_schedule_guard_hits = 0
        self._trigger_zero_schedule_reasons: Dict[str, int] = {}
        self._mempool_seen_total = 0
        self._mempool_watched_total = 0
        self._mempool_decoded_total = 0
        self._mempool_ignored_not_watched = 0
        self._mempool_ignored_selector = 0
        self._mempool_decode_failed = 0
        self._ignored_to_counts: Dict[str, int] = {}
        self._decoded_by_router: Dict[str, int] = {}
        self._ignored_by_reason: Dict[str, int] = {}
        self._trigger_drop_reasons: Dict[str, int] = {}
        self._decoded_ts: Deque[int] = deque(maxlen=2000)
        self._token_cache = TokenCache(Path("cache") / "token_metadata.json")
        self._liquidity_cache: Dict[str, Dict[str, Any]] = {}
        self._liquidity_cache_ttl_s = int(getattr(config, "MEMPOOL_LIQUIDITY_CACHE_TTL_S", 600))

    async def start(self, scan_fn: ScanFn) -> None:
        if self._running:
            return
        self._scan_fn = scan_fn
        self._running = True
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=max(1, self.trigger_queue_max))
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
        payload["triggers_queued"] = int(self._queue.qsize()) if self._queue else 0
        payload["triggers_running"] = int(self._running_triggers)
        payload["current_block"] = int(self._current_block or 0)
        payload["total_triggers_seen"] = int(self._triggers_seen)
        payload["total_triggers_processed"] = int(self._triggers_processed)
        payload["total_triggers_dropped_ttl"] = int(self._triggers_dropped_ttl)
        payload["total_triggers_dropped_queue"] = int(self._triggers_dropped_queue)
        payload["decoded_swaps_total"] = int(self._decoded_swaps_total)
        payload["post_validations_total"] = int(self._post_validations_total)
        payload["persistent_hits_total"] = int(self._persistent_hits)
        payload["valid_hits_total"] = int(self._valid_hits)
        payload["trigger_scans_scheduled"] = int(self._trigger_scans_scheduled)
        payload["trigger_scans_finished"] = int(self._trigger_scans_finished)
        payload["trigger_scans_timeouts"] = int(self._trigger_scans_timeouts)
        payload["trigger_scans_zero_candidates"] = int(self._trigger_scans_zero_candidates)
        payload["trigger_prepare_budget_ms"] = int(self.trigger_prepare_budget_ms)
        payload["trigger_prepare_truncated_count"] = int(self._trigger_prepare_truncated)
        payload["trigger_schedule_guard_hits"] = int(self._trigger_schedule_guard_hits)
        payload["trigger_zero_schedule_reasons"] = dict(self._trigger_zero_schedule_reasons)
        payload["mempool_seen_total"] = int(self._mempool_seen_total)
        payload["mempool_watched_total"] = int(self._mempool_watched_total)
        payload["mempool_decoded_total"] = int(self._mempool_decoded_total)
        payload["mempool_ignored_not_watched"] = int(self._mempool_ignored_not_watched)
        payload["mempool_ignored_selector"] = int(self._mempool_ignored_selector)
        payload["mempool_decode_failed"] = int(self._mempool_decode_failed)
        payload["mempool_watch_mode"] = self.watch_mode
        payload["mempool_watched_router_set"] = self.router_set
        payload["mempool_watched_router_names"] = list(self._watched_router_names)
        payload["mempool_top_ignored_to"] = self._top_counts(self._ignored_to_counts)
        payload["mempool_top_watched_routers"] = self._top_counts(self._decoded_by_router)
        payload["mempool_ignored_by_reason"] = dict(self._ignored_by_reason)
        payload["trigger_dropped_by_reason"] = dict(self._trigger_drop_reasons)
        payload["trigger_dropped_total"] = int(sum(self._trigger_drop_reasons.values())) if self._trigger_drop_reasons else 0
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - 60_000
        while self._decoded_ts and self._decoded_ts[0] < cutoff:
            self._decoded_ts.popleft()
        payload["decoded_swaps_last_minute"] = int(len(self._decoded_ts))
        if self._post_validations_total > 0:
            ratio = float(self._persistent_hits) / float(self._post_validations_total)
        else:
            ratio = 0.0
        payload["persistent_hit_ratio"] = float(ratio)
        payload["persistent_hit_warning"] = bool(self._post_validations_total >= 3 and ratio >= 0.5)
        if self._trigger_latency_count > 0:
            payload["avg_trigger_latency_ms"] = float(self._trigger_latency_sum_ms / self._trigger_latency_count)
        else:
            payload["avg_trigger_latency_ms"] = 0.0
        self._write_json(self.status_path, payload)
        if self.ui_push:
            await self.ui_push({"type": "mempool_status", **payload})

    async def _on_pending_tx(self, tx: PendingTx) -> None:
        to_addr = (tx.to_addr or "").lower()
        self._mempool_seen_total += 1
        router_info = self.router_registry.get(to_addr)
        router_name = router_info.name if router_info else None
        router_type = router_info.dex_type if router_info else None
        watched = bool(router_info)
        selector_match = is_known_selector(tx.input)

        if self.watch_mode == "routers_only":
            if not watched:
                self._mempool_ignored_not_watched += 1
                self._note_ignored(to_addr)
                self._bump_reason(self._ignored_by_reason, REASON_NOT_WATCHED_TO)
                await self._log_mempool(
                    tx,
                    status="ignored",
                    reason=REASON_NOT_WATCHED_TO,
                    watched=False,
                    router_name=None,
                    router_type=None,
                    ignore_stage="to_not_watched",
                )
                return
        else:
            if not watched and not selector_match:
                self._mempool_ignored_selector += 1
                self._note_ignored(to_addr)
                self._bump_reason(self._ignored_by_reason, REASON_IGNORED_SELECTOR)
                await self._log_mempool(
                    tx,
                    status="ignored",
                    reason=REASON_IGNORED_SELECTOR,
                    watched=False,
                    router_name=None,
                    router_type=None,
                    ignore_stage="selector_not_swap",
                )
                return

        if watched:
            self._mempool_watched_total += 1
        decoders = self.decoders if watched else self.decoders_any
        decoded, reason = decode_pending_tx(decoders, tx)
        if not decoded:
            self._mempool_decode_failed += 1
            self._bump_reason(self._ignored_by_reason, REASON_DECODE_FAILED)
            await self._log_mempool(
                tx,
                status="failed",
                reason=REASON_DECODE_FAILED,
                watched=watched,
                router_name=router_name,
                router_type=router_type,
                ignore_stage="decode_failed",
            )
            return
        summary = self._decoded_summary(decoded)
        self._decoded_swaps_total += 1
        self._decoded_ts.append(int(time.time() * 1000))
        self._mempool_decoded_total += 1
        if router_name:
            self._decoded_by_router[router_name] = self._decoded_by_router.get(router_name, 0) + 1
        self._recent_swaps.append(summary)
        self._write_json(self.recent_path, list(self._recent_swaps))
        if self.ui_push:
            await self.ui_push({"type": "mempool_swap", **summary})

        tokens = self._extract_tokens(decoded)
        has_anchor = any(self._is_anchor(t) for t in tokens)
        unknown_tokens = [t for t in tokens if t and not config.is_known_token(t)]
        unknown_metadata = []
        if unknown_tokens:
            for token in unknown_tokens:
                _, _, meta_reason = await resolve_token_metadata(
                    self.rpc,
                    token,
                    cache=self._token_cache,
                    timeout_s=self.confirm_timeout_s,
                )
                if meta_reason:
                    unknown_metadata.append(token)
        strict_unknown = bool(getattr(config, "MEMPOOL_STRICT_UNKNOWN_TOKENS", False))
        if unknown_metadata and strict_unknown:
            self._bump_reason(self._trigger_drop_reasons, REASON_TRIGGER_UNKNOWN_METADATA)
            METRICS.inc_reason("drop_reason_counts", "unknown_token", 1)
            await self._log_mempool(
                tx,
                status="ignored",
                reason=REASON_TRIGGER_UNKNOWN_METADATA,
                decoded_summary=summary,
                watched=watched,
                router_name=router_name,
                router_type=router_type,
                ignore_stage="trigger_filtered",
                trigger_value_usd=None,
                trigger_unknown_value=True,
            )
            return

        allow_no_anchor = False
        if tokens and not has_anchor:
            connectors = self._normalized_connectors()
            has_liq = False
            for token in tokens:
                if self._is_anchor(token):
                    continue
                if await self._has_liquidity_cached(token, connectors):
                    has_liq = True
                    break
            if not has_liq:
                self._bump_reason(self._trigger_drop_reasons, REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY)
                METRICS.inc_reason("drop_reason_counts", "unknown_token", 1)
                await self._log_mempool(
                    tx,
                    status="ignored",
                    reason=REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY,
                    decoded_summary=summary,
                    watched=watched,
                    router_name=router_name,
                    router_type=router_type,
                    ignore_stage="trigger_filtered",
                    trigger_value_usd=None,
                    trigger_unknown_value=True,
                )
                return
            allow_no_anchor = True

        trigger, reason = build_trigger(
            decoded,
            min_value_usd=self.min_value_usd,
            usd_per_eth=self.usd_per_eth,
            pending_tx=tx,
            allow_no_anchor=allow_no_anchor,
        )
        if not trigger:
            mapped = self._map_trigger_reason_code(reason)
            self._bump_reason(self._trigger_drop_reasons, mapped)
            metric_reason = self._map_drop_reason_metric(mapped)
            if metric_reason:
                METRICS.inc_reason("drop_reason_counts", metric_reason, 1)
            await self._log_mempool(
                tx,
                status="ignored",
                reason=mapped,
                decoded_summary=summary,
                watched=watched,
                router_name=router_name,
                router_type=router_type,
                ignore_stage=self._map_trigger_ignore_stage(mapped),
                trigger_value_usd=None,
                trigger_unknown_value=None,
            )
            return

        if not self._queue:
            self._queue = asyncio.Queue(maxsize=max(1, self.trigger_queue_max))
        if self._queue.full():
            self._triggers_dropped_queue += 1
            self._bump_reason(self._trigger_drop_reasons, REASON_TRIGGER_QUEUE_FULL)
            METRICS.inc_reason("drop_reason_counts", "time_budget_exhausted", 1)
            await self._log_mempool(
                tx,
                status="ignored",
                reason=REASON_TRIGGER_QUEUE_FULL,
                decoded_summary=summary,
                watched=watched,
                router_name=router_name,
                router_type=router_type,
                ignore_stage="queue_full",
                trigger_value_usd=trigger.usd_value,
                trigger_unknown_value=trigger.unknown_value,
            )
            return

        try:
            self._queue.put_nowait(trigger)
        except Exception:
            self._triggers_dropped_queue += 1
            self._bump_reason(self._trigger_drop_reasons, REASON_TRIGGER_ENQUEUE_FAILED)
            METRICS.inc_reason("drop_reason_counts", "internal_error", 1)
            await self._log_mempool(
                tx,
                status="ignored",
                reason=REASON_TRIGGER_ENQUEUE_FAILED,
                decoded_summary=summary,
                watched=watched,
                router_name=router_name,
                router_type=router_type,
                ignore_stage="queue_error",
                trigger_value_usd=trigger.usd_value,
                trigger_unknown_value=trigger.unknown_value,
            )
            return

        self._triggers_seen += 1
        await self._log_mempool(
            tx,
            status="decoded",
            reason=None,
            decoded_summary=summary,
            watched=watched,
            router_name=router_name,
            router_type=router_type,
            ignore_stage=None,
            trigger_value_usd=trigger.usd_value,
            trigger_unknown_value=trigger.unknown_value,
        )

    async def _trigger_worker(self) -> None:
        while self._running:
            try:
                if not self._queue:
                    await asyncio.sleep(0.01)
                    continue
                trigger = await self._queue.get()
            except asyncio.CancelledError:
                break
            if not trigger:
                continue
            now_ms = int(time.time() * 1000)
            if now_ms - trigger.created_at_ms > int(self.trigger_ttl_s * 1000):
                self._triggers_dropped_ttl += 1
                self._bump_reason(self._trigger_drop_reasons, REASON_TRIGGER_TTL_EXPIRED)
                METRICS.inc_reason("drop_reason_counts", "time_budget_exhausted", 1)
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
                self._triggers_processed += 1
                self._trigger_latency_sum_ms += max(0.0, float(time.time() * 1000) - float(trigger.created_at_ms))
                self._trigger_latency_count += 1
            except Exception:
                continue
            finally:
                self._running_triggers = max(0, self._running_triggers - 1)

    @staticmethod
    def _norm_token(token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        try:
            addr = config.token_address(token)
        except Exception:
            addr = str(token)
        return str(addr).lower()

    def _extract_tokens(self, decoded: DecodedSwap) -> List[str]:
        tokens_raw: List[str] = []
        if decoded.path:
            tokens_raw.extend(decoded.path)
        if decoded.token_in:
            tokens_raw.append(decoded.token_in)
        if decoded.token_out:
            tokens_raw.append(decoded.token_out)
        tokens: List[str] = []
        for t in tokens_raw:
            t_norm = self._norm_token(t)
            if not t_norm:
                continue
            if t_norm not in tokens:
                tokens.append(t_norm)
        return tokens

    def _normalized_connectors(self) -> List[str]:
        connectors: List[str] = []
        for t in getattr(config, "MEMPOOL_TRIGGER_CONNECTORS", []) or []:
            t_norm = self._norm_token(t)
            if not t_norm or t_norm in connectors:
                continue
            connectors.append(t_norm)
        return connectors

    def _is_anchor(self, token: str) -> bool:
        addr = str(token or "").lower()
        stable_set = {str(x).lower() for x in (getattr(config, "MEMPOOL_STABLE_SET", []) or []) if str(x).strip()}
        weth = str(getattr(config, "MEMPOOL_WETH", "") or "").lower()
        return bool(addr) and (addr in stable_set or addr == weth)

    async def _has_liquidity_cached(self, token: str, connectors: List[str]) -> bool:
        addr = str(token or "").lower()
        if not addr:
            return False
        now = time.time()
        cached = self._liquidity_cache.get(addr)
        if cached:
            if (now - float(cached.get("ts", 0.0))) <= float(self._liquidity_cache_ttl_s):
                return bool(cached.get("has", False))
        ok = await has_liquidity(self.rpc, addr, connectors, timeout_s=self.confirm_timeout_s)
        self._liquidity_cache[addr] = {"has": bool(ok), "ts": now}
        return bool(ok)

    def _decoded_summary(self, decoded: DecodedSwap) -> Dict[str, Any]:
        def _fmt_int(v: Optional[int]) -> Optional[str]:
            if v is None:
                return None
            try:
                return str(int(v))
            except Exception:
                return None
        return {
            "hash": decoded.tx_hash,
            "kind": decoded.kind,
            "router": decoded.router,
            "token_in": decoded.token_in,
            "token_out": decoded.token_out,
            "amount_in": _fmt_int(decoded.amount_in),
            "amount_out_min": _fmt_int(decoded.amount_out_min),
            "path": decoded.path,
            "fee_tiers": decoded.fee_tiers,
            "seen_at_ms": decoded.seen_at_ms,
        }

    def _append_trigger_result(self, record: TriggerRecord, post_result: Optional[Dict[str, Any]]) -> None:
        pre = record.pre_result or {}
        scheduled = int(pre.get("scheduled", 0) or 0)
        finished = int(pre.get("finished", 0) or 0)
        timeouts = int(pre.get("timeouts", 0) or 0)
        if post_result is None:
            self._trigger_scans_scheduled += scheduled
            self._trigger_scans_finished += finished
            self._trigger_scans_timeouts += timeouts
            if scheduled == 0 and finished == 0:
                self._trigger_scans_zero_candidates += 1
            if pre.get("prepare_truncated"):
                self._trigger_prepare_truncated += 1
            if pre.get("schedule_guard_triggered"):
                self._trigger_schedule_guard_hits += 1
            zero_reason = pre.get("zero_schedule_reason") or pre.get("zero_candidates_reason")
            if zero_reason:
                key = str(zero_reason)
                self._trigger_zero_schedule_reasons[key] = int(self._trigger_zero_schedule_reasons.get(key, 0)) + 1
        trigger_amount_in = None
        if record.trigger.amount_in is not None:
            try:
                trigger_amount_in = str(int(record.trigger.amount_in))
            except Exception:
                trigger_amount_in = None
        classification = pre.get("classification")
        if post_result:
            try:
                pre_net = float(pre.get("best_net", 0.0))
                post_net = float(post_result.get("best_net", 0.0))
                if pre_net > 0 and post_net <= 0:
                    classification = "VALID_HIT"
                elif pre_net > 0 and post_net > 0:
                    classification = "PERSISTENT_HIT"
            except Exception:
                pass
        post_delta = None
        if post_result:
            try:
                post_delta = float(pre.get("best_net", 0.0)) - float(post_result.get("best_net", 0.0))
            except Exception:
                post_delta = None
        if post_result:
            self._post_validations_total += 1
            if classification == "PERSISTENT_HIT":
                self._persistent_hits += 1
            elif classification == "VALID_HIT":
                self._valid_hits += 1
        entry = {
            "trigger_id": record.trigger.trigger_id,
            "tx_hash": record.trigger.tx_hash,
            "scheduled": scheduled,
            "finished": finished,
            "timeouts": timeouts,
            "candidates_raw": pre.get("candidates_raw"),
            "candidates_after_prepare": pre.get("candidates_after_prepare"),
            "candidates_after_universe": pre.get("candidates_after_universe"),
            "candidates_after_base_filter": pre.get("candidates_after_base_filter"),
            "candidates_after_hops_filter": pre.get("candidates_after_hops_filter"),
            "candidates_after_cross_dex_filter": pre.get("candidates_after_cross_dex_filter"),
            "candidates_after_viability_filter": pre.get("candidates_after_viability_filter"),
            "candidates_after_caps": pre.get("candidates_after_caps"),
            "candidates_scheduled": pre.get("candidates_scheduled"),
            "zero_candidates_reason": pre.get("zero_candidates_reason"),
            "zero_candidates_detail": pre.get("zero_candidates_detail"),
            "zero_candidates_stage": pre.get("zero_candidates_stage"),
            "zero_schedule_reason": pre.get("zero_schedule_reason"),
            "zero_schedule_detail": pre.get("zero_schedule_detail"),
            "base_used": pre.get("base_used"),
            "base_fallback_used": pre.get("base_fallback_used"),
            "hops_attempted": pre.get("hops_attempted"),
            "hop_fallback_used": pre.get("hop_fallback_used"),
            "connectors_added": pre.get("connectors_added"),
            "cross_dex_fallback_used": pre.get("cross_dex_fallback_used"),
            "prepare_truncated": pre.get("prepare_truncated"),
            "prepare_time_ms": pre.get("prepare_time_ms"),
            "schedule_guard_remaining_ms": pre.get("schedule_guard_remaining_ms"),
            "schedule_guard_blocked": pre.get("schedule_guard_blocked"),
            "schedule_guard_triggered": pre.get("schedule_guard_triggered"),
            "schedule_guard_reason": pre.get("schedule_guard_reason"),
            "budget_remaining_ms_at_schedule": pre.get("budget_remaining_ms_at_schedule"),
            "viability_rejects_by_reason": pre.get("viability_rejects_by_reason"),
            "capped_by_trigger_max_candidates_raw": pre.get("capped_by_trigger_max_candidates_raw"),
            "best_gross": float(pre.get("best_gross", 0.0)),
            "best_net": float(pre.get("best_net", 0.0)),
            "best_route": pre.get("best_route_summary"),
            "best_route_tokens": pre.get("best_route_tokens"),
            "best_hops": pre.get("best_hops"),
            "best_hop_amounts": pre.get("best_hop_amounts"),
            "best_amount_in": pre.get("best_amount_in"),
            "candidate_id": pre.get("candidate_id"),
            "outcome": pre.get("outcome", "no_hit"),
            "classification": classification,
            "backend": pre.get("backend"),
            "sim_backend_used": pre.get("backend"),
            "sim_ok": pre.get("sim_ok"),
            "sim_revert_reason": pre.get("sim_revert_reason"),
            "dex_mix": pre.get("dex_mix"),
            "hops": pre.get("hops"),
            "reason_selected": pre.get("reason_selected"),
            "arb_calldata_len": pre.get("arb_calldata_len"),
            "arb_calldata_prefix": pre.get("arb_calldata_prefix"),
            "trigger_amount_in": trigger_amount_in,
            "token_universe": record.trigger.token_universe,
            "ts": int(time.time() * 1000),
            "pre_block": record.pre_block,
            "post_block": record.post_block,
            "post_best_net": record.post_best_net,
            "post_delta_net": post_delta,
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
        watched: Optional[bool] = None,
        router_name: Optional[str] = None,
        router_type: Optional[str] = None,
        ignore_stage: Optional[str] = None,
        trigger_value_usd: Optional[str] = None,
        trigger_unknown_value: Optional[bool] = None,
    ) -> None:
        entry = {
            "ts": int(time.time() * 1000),
            "hash": tx.tx_hash,
            "to": tx.to_addr,
            "status": status,
            "reason": reason,
            "watched": watched,
            "router_name": router_name,
            "router_type": router_type,
            "ignore_stage": ignore_stage,
            "decoded_summary": decoded_summary,
            "trigger_value_usd": trigger_value_usd,
            "trigger_unknown_value": trigger_unknown_value,
        }
        try:
            self.mempool_log.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
        except Exception:
            pass

    @staticmethod
    def _top_counts(counter: Dict[str, int], limit: int = 5) -> List[Dict[str, Any]]:
        items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
        return [{"key": k, "count": int(v)} for k, v in items[:limit]]

    def _note_ignored(self, to_addr: str) -> None:
        if not to_addr:
            return
        self._ignored_to_counts[to_addr] = self._ignored_to_counts.get(to_addr, 0) + 1

    @staticmethod
    def _map_trigger_reason_code(reason: Optional[str]) -> str:
        if not reason:
            return "trigger_filtered"
        if reason == "below_usd_threshold":
            return REASON_TRIGGER_BELOW_USD
        if reason == "unknown_value_strict":
            return REASON_TRIGGER_UNKNOWN_VALUE
        if reason == "below_raw_threshold":
            return REASON_TRIGGER_BELOW_RAW
        if reason == "missing_amount_in":
            return REASON_TRIGGER_MISSING_AMOUNT
        if reason == "unknown_metadata":
            return REASON_TRIGGER_UNKNOWN_METADATA
        if reason == "unknown_no_liquidity":
            return REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY
        if reason in ("unknown_not_in_universe", "unknown_tokens"):
            return REASON_TRIGGER_UNKNOWN_NOT_IN_UNIVERSE
        return "trigger_filtered"

    @staticmethod
    def _map_trigger_ignore_stage(reason_code: str) -> str:
        if reason_code in (REASON_TRIGGER_BELOW_USD, REASON_TRIGGER_BELOW_RAW, REASON_TRIGGER_MISSING_AMOUNT):
            return "tiny_swap"
        if reason_code in (REASON_TRIGGER_UNKNOWN_VALUE, REASON_TRIGGER_UNKNOWN_METADATA, REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY, REASON_TRIGGER_UNKNOWN_NOT_IN_UNIVERSE):
            return "trigger_filtered"
        return "trigger_filtered"

    @staticmethod
    def _map_drop_reason_metric(reason_code: str) -> Optional[str]:
        if reason_code in (
            REASON_TRIGGER_UNKNOWN_VALUE,
            REASON_TRIGGER_UNKNOWN_METADATA,
            REASON_TRIGGER_UNKNOWN_NO_LIQUIDITY,
            REASON_TRIGGER_UNKNOWN_NOT_IN_UNIVERSE,
            REASON_TRIGGER_MISSING_AMOUNT,
        ):
            return "unknown_token"
        if reason_code in (REASON_TRIGGER_BELOW_USD, REASON_TRIGGER_BELOW_RAW):
            return "min_profit_not_met"
        if reason_code in (REASON_TRIGGER_QUEUE_FULL, REASON_TRIGGER_TTL_EXPIRED):
            return "time_budget_exhausted"
        if reason_code == REASON_TRIGGER_ENQUEUE_FAILED:
            return "internal_error"
        return None

    @staticmethod
    def _bump_reason(counter: Dict[str, int], reason: str) -> None:
        if not reason:
            return
        counter[reason] = int(counter.get(reason, 0)) + 1

    def _write_json(self, path: Path, payload: Any) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass
