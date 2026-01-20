import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


DEFAULT_INTERVAL_S = 45.0
DEFAULT_WINDOW_S = 900
TAIL_MAX_BYTES = 2_000_000
TAIL_MAX_LINES = 5000


def _mask_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return raw
    scheme = ""
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    host = rest.split("/", 1)[0]
    if scheme:
        return f"{scheme}://{host}/..."
    return host


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except Exception:
        return None
    if not math.isfinite(f):
        return None
    return f


def _format_amount(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, str):
        return value or None
    f = _safe_float(value)
    if f is None:
        return None
    text = f"{f:.12f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return text


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl_tail(path: Path, *, max_bytes: int = TAIL_MAX_BYTES, max_lines: int = TAIL_MAX_LINES) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        if size <= 0:
            return []
        read_size = min(size, int(max_bytes))
        with path.open("rb") as f:
            f.seek(max(0, size - read_size))
            data = f.read(read_size)
        lines = data.splitlines()
        if size > read_size and lines:
            lines = lines[1:]
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        out: List[Dict[str, Any]] = []
        for line in lines:
            try:
                if not line:
                    continue
                obj = json.loads(line.decode("utf-8"))
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
    except Exception:
        return []


def _infer_current_block(log_dir: Path) -> Optional[int]:
    status = _read_json(log_dir / "mempool_status.json")
    if isinstance(status, dict):
        val = _safe_int(status.get("current_block"))
        if val is not None:
            return val
    entries = _read_jsonl_tail(log_dir / "blocks.jsonl", max_lines=5)
    if entries:
        val = _safe_int(entries[-1].get("block"))
        if val is not None:
            return val
    return None


def _rolling_trigger_stats(entries: List[Dict[str, Any]], now_ms: int, window_s: int) -> Dict[str, Any]:
    window_ms = int(window_s) * 1000
    window_entries = [
        e for e in entries
        if isinstance(e, dict) and _safe_int(e.get("ts")) is not None and _safe_int(e.get("ts")) >= (now_ms - window_ms)
    ]
    count = len(window_entries)
    scheduled_zero = 0
    gross_hits = 0
    net_hits = 0
    valid_hits = 0
    persistent_hits = 0
    best_nets: List[float] = []
    for e in window_entries:
        cls = str(e.get("classification") or e.get("outcome") or "").strip()
        if cls in ("gross_hit", "net_hit", "VALID_HIT", "PERSISTENT_HIT"):
            gross_hits += 1
        if cls in ("net_hit", "VALID_HIT", "PERSISTENT_HIT"):
            net_hits += 1
        if cls == "VALID_HIT":
            valid_hits += 1
        if cls == "PERSISTENT_HIT":
            persistent_hits += 1
        if _safe_int(e.get("scheduled")) == 0:
            scheduled_zero += 1
        bn = _safe_float(e.get("best_net"))
        if bn is not None:
            best_nets.append(bn)
    avg_net = None
    worst_net = None
    best_net = None
    if best_nets:
        avg_net = sum(best_nets) / float(len(best_nets))
        worst_net = min(best_nets)
        best_net = max(best_nets)
    pct_zero = None
    if count > 0:
        pct_zero = (float(scheduled_zero) / float(count)) * 100.0
    return {
        "window_s": int(window_s),
        "trigger_scans_count": int(count),
        "gross_hits_count": int(gross_hits),
        "net_hits_count": int(net_hits),
        "valid_hits_count": int(valid_hits),
        "persistent_hits_count": int(persistent_hits),
        "avg_best_net": _format_amount(avg_net),
        "worst_best_net": _format_amount(worst_net),
        "best_best_net": _format_amount(best_net),
        "percent_scheduled_zero": pct_zero,
    }


def _summarize_last_trigger(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entry or not isinstance(entry, dict):
        return None
    return {
        "tx_hash": entry.get("tx_hash"),
        "trigger_amount_in": _format_amount(entry.get("trigger_amount_in")),
        "token_universe": entry.get("token_universe"),
        "scheduled": _safe_int(entry.get("scheduled")),
        "finished": _safe_int(entry.get("finished")),
        "best_route": entry.get("best_route"),
        "hops": entry.get("hops"),
        "dex_mix": entry.get("dex_mix"),
        "backend": entry.get("backend"),
        "best_gross": _format_amount(entry.get("best_gross")),
        "best_net": _format_amount(entry.get("best_net")),
        "classification": entry.get("classification") or entry.get("outcome"),
        "reason_selected": entry.get("reason_selected"),
        "pre_block": _safe_int(entry.get("pre_block")),
        "post_block": _safe_int(entry.get("post_block")),
        "post_best_net": _format_amount(entry.get("post_best_net")),
        "post_delta_net": _format_amount(entry.get("post_delta_net")),
        "zero_candidates_reason": entry.get("zero_candidates_reason"),
        "zero_candidates_stage": entry.get("zero_candidates_stage"),
    }


def build_diagnostic_snapshot(
    *,
    log_dir: Path,
    settings: Any,
    session_started_at_s: Optional[float],
    current_block: Optional[int],
    rpc_stats: Optional[List[Dict[str, Any]]],
    window_s: int = DEFAULT_WINDOW_S,
    update_reason: Optional[str] = None,
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    if current_block is None:
        current_block = _infer_current_block(log_dir)
    uptime_s = None
    if session_started_at_s is not None:
        uptime_s = int(max(0, time.time() - float(session_started_at_s)))

    sim_profile = str(os.getenv("SIM_PROFILE", "")).strip() or None
    gas_off = str(os.getenv("GAS_OFF", "")).strip().lower() in ("1", "true", "yes", "on")
    fixed_gas = int(os.getenv("FIXED_GAS_UNITS", "0") or 0)
    if gas_off:
        gas_mode = "off"
    elif fixed_gas > 0:
        gas_mode = "fixed"
    else:
        gas_mode = "dynamic"

    scan_source = str(getattr(settings, "scan_source", "block") or "block").strip().lower()

    mempool_status = _read_json(log_dir / "mempool_status.json") or {}
    mempool_triggers = _read_json(log_dir / "mempool_triggers.json") or []
    last_trigger_entry = mempool_triggers[-1] if mempool_triggers else None
    last_trigger = _summarize_last_trigger(last_trigger_entry)

    mempool_log = _read_jsonl_tail(log_dir / "mempool.jsonl")
    trigger_log = _read_jsonl_tail(log_dir / "trigger_scans.jsonl")

    decoded_last_minute = None
    if mempool_log:
        cutoff = now_ms - 60_000
        decoded_last_minute = sum(
            1 for e in mempool_log
            if isinstance(e, dict) and e.get("status") == "decoded" and _safe_int(e.get("ts")) is not None and _safe_int(e.get("ts")) >= cutoff
        )

    drop_reasons: Dict[str, int] = {}
    if mempool_log:
        cutoff = now_ms - int(window_s) * 1000
        for e in mempool_log:
            if not isinstance(e, dict):
                continue
            ts = _safe_int(e.get("ts"))
            if ts is None or ts < cutoff:
                continue
            if e.get("status") != "ignored":
                continue
            reason = str(e.get("reason") or "").strip()
            if not reason:
                continue
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1

    rpc_ok = None
    rpc_err = None
    rpc_timeout = None
    rpc_avg_lat = None
    rpc_urls: Optional[List[str]] = None
    if rpc_stats:
        rpc_urls = [_mask_url(s.get("url", "")) for s in rpc_stats if isinstance(s, dict)]
        ok_vals = [int(s.get("ok", 0)) for s in rpc_stats if isinstance(s, dict)]
        fail_vals = [int(s.get("fail", 0)) for s in rpc_stats if isinstance(s, dict)]
        timeout_vals = [int(s.get("timeout", 0)) for s in rpc_stats if isinstance(s, dict) and s.get("timeout") is not None]
        lat_vals = [float(s.get("lat_ms", 0.0)) for s in rpc_stats if isinstance(s, dict) and s.get("lat_ms") is not None]
        rpc_ok = sum(ok_vals) if ok_vals else None
        rpc_err = sum(fail_vals) if fail_vals else None
        rpc_timeout = sum(timeout_vals) if timeout_vals else None
        if lat_vals:
            rpc_avg_lat = sum(lat_vals) / float(len(lat_vals))

    total_dropped = None
    dropped_queue = _safe_int(mempool_status.get("total_triggers_dropped_queue")) if isinstance(mempool_status, dict) else None
    dropped_ttl = _safe_int(mempool_status.get("total_triggers_dropped_ttl")) if isinstance(mempool_status, dict) else None
    if dropped_queue is not None or dropped_ttl is not None:
        total_dropped = int((dropped_queue or 0) + (dropped_ttl or 0))

    pipeline = {
        "decoded_swaps_total": _safe_int(mempool_status.get("decoded_swaps_total")) if isinstance(mempool_status, dict) else None,
        "decoded_swaps_last_minute": decoded_last_minute,
        "triggers_seen": _safe_int(mempool_status.get("total_triggers_seen")) if isinstance(mempool_status, dict) else None,
        "triggers_queued": _safe_int(mempool_status.get("triggers_queued")) if isinstance(mempool_status, dict) else None,
        "triggers_dropped": {
            "total": total_dropped,
            "by_reason": drop_reasons,
            "window_s": int(window_s),
        },
        "trigger_scans_scheduled": _safe_int(mempool_status.get("trigger_scans_scheduled")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_finished": _safe_int(mempool_status.get("trigger_scans_finished")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_timeouts": _safe_int(mempool_status.get("trigger_scans_timeouts")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_zero_candidates": _safe_int(mempool_status.get("trigger_scans_zero_candidates")) if isinstance(mempool_status, dict) else None,
        "last_zero_candidates_reason": last_trigger_entry.get("zero_candidates_reason") if isinstance(last_trigger_entry, dict) else None,
        "last_zero_candidates_stage": last_trigger_entry.get("zero_candidates_stage") if isinstance(last_trigger_entry, dict) else None,
    }

    rolling = _rolling_trigger_stats(trigger_log, now_ms, int(window_s))

    config_snapshot = {
        "routing_search": {
            "max_hops": _safe_int(getattr(settings, "max_hops", None)),
            "beam_k": _safe_int(getattr(settings, "beam_k", None)),
            "edge_top_m": _safe_int(getattr(settings, "edge_top_m", None)),
            "dexes": list(getattr(settings, "dexes", []) or []),
            "enable_multidex": bool(getattr(settings, "enable_multidex", False)),
            "trigger_prefer_cross_dex": bool(getattr(settings, "trigger_prefer_cross_dex", False)),
            "trigger_require_cross_dex": bool(getattr(settings, "trigger_require_cross_dex", False)),
            "trigger_require_three_hops": bool(getattr(settings, "trigger_require_three_hops", False)),
        },
        "mempool": {
            "mempool_min_value_usd": _format_amount(getattr(settings, "mempool_min_value_usd", None)),
            "mempool_trigger_scan_budget_s": _safe_float(getattr(settings, "mempool_trigger_scan_budget_s", None)),
            "mempool_fetch_tx_concurrency": _safe_int(getattr(settings, "mempool_fetch_tx_concurrency", None)),
        },
        "simulation": {
            "slippage_bps": _safe_float(getattr(settings, "slippage_bps", None)),
            "mev_buffer_bps": _safe_float(getattr(settings, "mev_buffer_bps", None)),
            "min_profit_abs": _format_amount(getattr(settings, "min_profit_abs", None)),
            "min_profit_pct": _safe_float(getattr(settings, "min_profit_pct", None)),
        },
        "rpc": {
            "concurrency": _safe_int(getattr(settings, "concurrency", None)),
            "rpc_timeout_stage1_s": _safe_float(getattr(settings, "rpc_timeout_stage1_s", None)),
            "rpc_timeout_stage2_s": _safe_float(getattr(settings, "rpc_timeout_stage2_s", None)),
        },
    }

    warning_scheduled_zero = None
    if rolling.get("trigger_scans_count", 0) and rolling.get("percent_scheduled_zero") is not None:
        warning_scheduled_zero = bool(rolling["trigger_scans_count"] >= 3 and rolling["percent_scheduled_zero"] >= 80.0)
    mempool_enabled = bool(getattr(settings, "mempool_enabled", False)) or scan_source in ("mempool", "hybrid")
    ws_connected = mempool_status.get("ws_connected") if isinstance(mempool_status, dict) else None
    warning_ws_unstable = None
    if mempool_enabled and ws_connected is not None:
        warning_ws_unstable = bool(not ws_connected or int(mempool_status.get("ws_reconnects", 0)) >= 3)
    fetch_pct = mempool_status.get("tx_fetch_success_rate") if isinstance(mempool_status, dict) else None
    fetch_pct = _safe_float(fetch_pct)
    warning_low_fetch = None
    if mempool_enabled and fetch_pct is not None:
        warning_low_fetch = bool(fetch_pct < 0.6)
    warning_persistent = None
    if isinstance(mempool_status, dict) and mempool_status.get("persistent_hit_warning") is not None:
        warning_persistent = bool(mempool_status.get("persistent_hit_warning"))

    last_block_entry = None
    block_entries = _read_jsonl_tail(log_dir / "blocks.jsonl", max_lines=5)
    if block_entries:
        last_block_entry = block_entries[-1]
    warning_no_routes = None
    if last_block_entry and isinstance(last_block_entry, dict):
        cand = _safe_int(last_block_entry.get("candidates_total", last_block_entry.get("candidates")))
        if cand is not None:
            warning_no_routes = bool(cand == 0)

    warnings = {
        "warning_scheduled_always_zero": warning_scheduled_zero,
        "warning_ws_unstable": warning_ws_unstable,
        "warning_low_fetch_success": warning_low_fetch,
        "warning_require_cross_dex_enabled": bool(getattr(settings, "trigger_require_cross_dex", False)),
        "warning_no_routes_generated": warning_no_routes,
        "warning_persistent_hits_detected": warning_persistent,
    }

    snapshot = {
        "schema_version": 1,
        "update_reason": update_reason,
        "global": {
            "timestamp_ms": int(now_ms),
            "uptime_seconds": uptime_s,
            "current_block": _safe_int(current_block),
            "scan_source": scan_source,
            "sim_profile": sim_profile,
            "gas_mode": gas_mode,
        },
        "rpc_ws": {
            "rpc_urls": rpc_urls,
            "rpc_ok_count": rpc_ok,
            "rpc_error_count": rpc_err,
            "rpc_timeout_count": rpc_timeout,
            "rpc_avg_latency_ms": rpc_avg_lat,
            "mempool_ws_connected": ws_connected if isinstance(ws_connected, bool) else None,
            "mempool_hash_rate": _safe_float(mempool_status.get("tx_hash_rate")) if isinstance(mempool_status, dict) else None,
            "mempool_fetch_success_pct": _safe_float(mempool_status.get("tx_fetch_success_rate")) if isinstance(mempool_status, dict) else None,
            "mempool_reconnects": _safe_int(mempool_status.get("ws_reconnects")) if isinstance(mempool_status, dict) else None,
        },
        "pipeline": pipeline,
        "last_trigger": last_trigger,
        "rolling": rolling,
        "config": config_snapshot,
        "warnings": warnings,
    }
    return snapshot


class DiagnosticSnapshotter:
    def __init__(
        self,
        *,
        log_dir: Path,
        get_settings: Callable[[], Any],
        get_current_block: Callable[[], Optional[int]],
        session_started_at_s: Optional[float],
        rpc_provider: Optional[Any] = None,
        interval_s: Optional[float] = None,
        window_s: Optional[int] = None,
        path: Optional[Path] = None,
    ) -> None:
        self.log_dir = log_dir
        self.get_settings = get_settings
        self.get_current_block = get_current_block
        self.session_started_at_s = session_started_at_s
        self.rpc_provider = rpc_provider
        self.interval_s = float(interval_s if interval_s is not None else DEFAULT_INTERVAL_S)
        self.window_s = int(window_s if window_s is not None else DEFAULT_WINDOW_S)
        self.path = path or (log_dir / "diagnostic_snapshot.json")
        self._running = False
        self._task = None

    def _rpc_stats(self) -> Optional[List[Dict[str, Any]]]:
        if not self.rpc_provider:
            return None
        try:
            if hasattr(self.rpc_provider, "stats"):
                stats = self.rpc_provider.stats()
                if isinstance(stats, list):
                    return stats
        except Exception:
            return None
        return None

    def write_snapshot(self, *, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        settings = self.get_settings()
        snapshot = build_diagnostic_snapshot(
            log_dir=self.log_dir,
            settings=settings,
            session_started_at_s=self.session_started_at_s,
            current_block=self.get_current_block(),
            rpc_stats=self._rpc_stats(),
            window_s=self.window_s,
            update_reason=reason,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(snapshot), encoding="utf-8")
            return snapshot
        except Exception:
            return None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                self.write_snapshot(reason="interval")
                await asyncio.sleep(float(self.interval_s))
            except Exception:
                await asyncio.sleep(float(self.interval_s))
