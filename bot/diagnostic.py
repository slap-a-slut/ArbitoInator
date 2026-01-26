import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from infra.metrics import METRICS


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


_ENDPOINT_HINTS = (
    ("publicnode", ["publicnode.com"]),
    ("llama", ["llamarpc.com"]),
    ("ankr", ["rpc.ankr.com"]),
    ("flashbots_calls", ["flashbots.net"]),
    ("getblock", ["getblock.us"]),
    ("merkle", ["merkle.io"]),
    ("0xrpc", ["0xrpc.io"]),
)


def _endpoint_id_from_url(url: str) -> str:
    raw = str(url or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    host = raw.split("/", 1)[0]
    for name, needles in _ENDPOINT_HINTS:
        if any(n in host for n in needles):
            return name
    if not host:
        return "rpc"
    return host.replace(":", "_")


def _endpoint_ids_from_settings(settings: Any, *, endpoints_attr: str, fallback_urls_attr: str) -> List[str]:
    endpoints = getattr(settings, endpoints_attr, None)
    ids: List[str] = []
    if isinstance(endpoints, (list, tuple)):
        for entry in endpoints:
            if isinstance(entry, dict):
                url = entry.get("url")
                eid = entry.get("id") or _endpoint_id_from_url(url)
                if eid:
                    ids.append(str(eid))
            elif isinstance(entry, str):
                ids.append(_endpoint_id_from_url(entry))
    if ids:
        return ids
    urls = getattr(settings, fallback_urls_attr, None)
    if isinstance(urls, (list, tuple)):
        return [_endpoint_id_from_url(u) for u in urls if u]
    return []


def _settings_endpoints(settings: Any, *, endpoints_attr: str, fallback_urls_attr: str) -> List[Dict[str, str]]:
    endpoints = getattr(settings, endpoints_attr, None)
    out: List[Dict[str, str]] = []
    if isinstance(endpoints, (list, tuple)):
        for entry in endpoints:
            if isinstance(entry, dict):
                url = str(entry.get("url") or "").strip()
                if not url:
                    continue
                eid = str(entry.get("id") or "").strip() or _endpoint_id_from_url(url)
                out.append({"id": eid, "url": url})
            elif isinstance(entry, str):
                url = entry.strip()
                if url:
                    out.append({"id": _endpoint_id_from_url(url), "url": url})
    if out:
        return out
    urls = getattr(settings, fallback_urls_attr, None)
    if isinstance(urls, (list, tuple)):
        for url in urls:
            if url:
                out.append({"id": _endpoint_id_from_url(url), "url": str(url)})
    return out


def _build_ws_http_pairing(ws_eps: List[Dict[str, str]], http_eps: List[Dict[str, str]]) -> Dict[str, str]:
    pairing: Dict[str, str] = {}
    http_by_id: Dict[str, str] = {}
    http_by_host: Dict[str, str] = {}
    for ep in http_eps:
        eid = str(ep.get("id") or "").strip()
        url = str(ep.get("url") or "").strip()
        if eid and url:
            http_by_id[eid] = url
        if url:
            host = url.split("://", 1)[-1].split("/", 1)[0].lower()
            http_by_host[host] = url
    for ep in ws_eps:
        wid = str(ep.get("id") or "").strip()
        wurl = str(ep.get("url") or "").strip()
        if not wurl:
            continue
        if wid and wid in http_by_id:
            pairing[wurl] = http_by_id[wid]
            continue
        host = wurl.split("://", 1)[-1].split("/", 1)[0].lower()
        if host in http_by_host:
            pairing[wurl] = http_by_host[host]
    return pairing


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
        if cls in ("gross_hit", "net_hit", "valid_hit", "VALID_HIT", "PERSISTENT_HIT"):
            gross_hits += 1
        if cls in ("net_hit", "valid_hit", "VALID_HIT", "PERSISTENT_HIT"):
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


def _aggregate_tx_ready(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = 0
    ready = 0
    reverted = 0
    skipped = 0
    drop_reasons: Dict[str, int] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        total += 1
        status = str(e.get("status") or "").lower()
        if status == "ready":
            ready += 1
        elif status == "reverted":
            reverted += 1
        else:
            skipped += 1
        reason = e.get("drop_reason")
        if reason:
            key = str(reason)
            drop_reasons[key] = int(drop_reasons.get(key, 0)) + 1
    return {
        "tx_ready_total": int(total),
        "tx_ready_ready": int(ready),
        "tx_ready_reverted": int(reverted),
        "tx_ready_skipped": int(skipped),
        "tx_drop_reasons": drop_reasons,
    }


def _summarize_last_trigger(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entry or not isinstance(entry, dict):
        return None
    top_viability = entry.get("top_viability_drop_reason")
    if not top_viability:
        try:
            rejects = entry.get("viability_rejects_by_reason") or {}
            if isinstance(rejects, dict) and rejects:
                top_viability = sorted(rejects.items(), key=lambda kv: int(kv[1]), reverse=True)[0][0]
        except Exception:
            top_viability = None
    return {
        "candidate_id": entry.get("candidate_id"),
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
        "top_viability_drop_reason": top_viability,
    }


def build_diagnostic_snapshot(
    *,
    log_dir: Path,
    settings: Any,
    session_started_at_s: Optional[float],
    current_block: Optional[int],
    rpc_stats: Optional[List[Dict[str, Any]]],
    rpc_health: Optional[Dict[str, Any]] = None,
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
    tx_ready_log = _read_jsonl_tail(log_dir / "tx_ready.jsonl")
    tx_stats = _aggregate_tx_ready(tx_ready_log)

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
    dropped_trigger = _safe_int(mempool_status.get("trigger_dropped_total")) if isinstance(mempool_status, dict) else None
    if dropped_trigger is not None:
        total_dropped = int(dropped_trigger)
    elif dropped_queue is not None or dropped_ttl is not None:
        total_dropped = int((dropped_queue or 0) + (dropped_ttl or 0))

    pipeline = {
        "decoded_swaps_total": _safe_int(mempool_status.get("decoded_swaps_total")) if isinstance(mempool_status, dict) else None,
        "decoded_swaps_last_minute": _safe_int(mempool_status.get("decoded_swaps_last_minute")) if isinstance(mempool_status, dict) else None,
        "triggers_seen": _safe_int(mempool_status.get("total_triggers_seen")) if isinstance(mempool_status, dict) else None,
        "triggers_queued": _safe_int(mempool_status.get("triggers_queued")) if isinstance(mempool_status, dict) else None,
        "mempool_seen_total": _safe_int(mempool_status.get("mempool_seen_total")) if isinstance(mempool_status, dict) else None,
        "mempool_watched_total": _safe_int(mempool_status.get("mempool_watched_total")) if isinstance(mempool_status, dict) else None,
        "mempool_decoded_total": _safe_int(mempool_status.get("mempool_decoded_total")) if isinstance(mempool_status, dict) else None,
        "mempool_ignored_not_watched": _safe_int(mempool_status.get("mempool_ignored_not_watched")) if isinstance(mempool_status, dict) else None,
        "mempool_ignored_selector": _safe_int(mempool_status.get("mempool_ignored_selector")) if isinstance(mempool_status, dict) else None,
        "mempool_decode_failed": _safe_int(mempool_status.get("mempool_decode_failed")) if isinstance(mempool_status, dict) else None,
        "mempool_top_ignored_to": mempool_status.get("mempool_top_ignored_to") if isinstance(mempool_status, dict) else None,
        "mempool_top_watched_routers": mempool_status.get("mempool_top_watched_routers") if isinstance(mempool_status, dict) else None,
        "mempool_ignored_by_reason": mempool_status.get("mempool_ignored_by_reason") if isinstance(mempool_status, dict) else None,
        "trigger_dropped_by_reason": mempool_status.get("trigger_dropped_by_reason") if isinstance(mempool_status, dict) else None,
        "triggers_dropped": {
            "total": total_dropped,
            "by_reason": mempool_status.get("trigger_dropped_by_reason") if isinstance(mempool_status, dict) else {},
            "window_s": int(window_s),
        },
        "trigger_scans_scheduled": _safe_int(mempool_status.get("trigger_scans_scheduled")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_finished": _safe_int(mempool_status.get("trigger_scans_finished")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_timeouts": _safe_int(mempool_status.get("trigger_scans_timeouts")) if isinstance(mempool_status, dict) else None,
        "trigger_scans_zero_candidates": _safe_int(mempool_status.get("trigger_scans_zero_candidates")) if isinstance(mempool_status, dict) else None,
        "trigger_prepare_budget_ms": _safe_int(mempool_status.get("trigger_prepare_budget_ms")) if isinstance(mempool_status, dict) else None,
        "trigger_prepare_truncated_count": _safe_int(mempool_status.get("trigger_prepare_truncated_count")) if isinstance(mempool_status, dict) else None,
        "trigger_schedule_guard_hits": _safe_int(mempool_status.get("trigger_schedule_guard_hits")) if isinstance(mempool_status, dict) else None,
        "trigger_zero_schedule_reasons": mempool_status.get("trigger_zero_schedule_reasons") if isinstance(mempool_status, dict) else None,
        "last_zero_candidates_reason": last_trigger_entry.get("zero_candidates_reason") if isinstance(last_trigger_entry, dict) else None,
        "last_zero_candidates_stage": last_trigger_entry.get("zero_candidates_stage") if isinstance(last_trigger_entry, dict) else None,
    }
    pipeline.update(tx_stats)

    rolling = _rolling_trigger_stats(trigger_log, now_ms, int(window_s))

    metrics_snapshot = METRICS.snapshot()
    counters = metrics_snapshot.get("counters", {})
    reasons = metrics_snapshot.get("reason_counters", {})
    hist = metrics_snapshot.get("histograms", {})

    rpc_latency = hist.get("rpc_latency_ms", {})
    rpc_latency_p50 = rpc_latency.get("p50")
    rpc_latency_p95 = rpc_latency.get("p95")
    rpc_latency_by_endpoint: Dict[str, Any] = {}
    for name, stats in hist.items():
        if not str(name).startswith("rpc_latency_ms:"):
            continue
        endpoint = str(name).split("rpc_latency_ms:", 1)[1]
        rpc_latency_by_endpoint[endpoint] = {
            "p50": stats.get("p50"),
            "p95": stats.get("p95"),
            "count": stats.get("count"),
        }

    metrics_section = {
        "quotes_total": int(counters.get("quotes_total", 0)),
        "quotes_ok": int(counters.get("quotes_ok", 0)),
        "quotes_fail_by_reason": reasons.get("quotes_fail_by_reason", {}),
        "rpc_requests_total": int(counters.get("rpc_requests_total", 0)),
        "rpc_requests_by_endpoint": reasons.get("rpc_requests_by_endpoint", {}),
        "rpc_fail_by_reason": reasons.get("rpc_fail_by_reason", {}),
        "rpc_fallbacks_by_reason": reasons.get("rpc_fallbacks_by_reason", {}),
        "rpc_latency_ms_p50": rpc_latency_p50,
        "rpc_latency_ms_p95": rpc_latency_p95,
        "rpc_latency_ms_by_endpoint": rpc_latency_by_endpoint,
        "out_of_sync_count": int(counters.get("out_of_sync_count", 0)),
        "rpc_not_caught_up_events": int(counters.get("rpc_not_caught_up_events", 0)),
        "pinned_violation_count": int(counters.get("pinned_violation_count", 0)),
        "candidates_generated": int(counters.get("candidates_generated", 0)),
        "candidates_after_probe": int(counters.get("candidates_after_probe", 0)),
        "candidates_after_refine": int(counters.get("candidates_after_refine", 0)),
        "preflight_ok": int(counters.get("preflight_ok", 0)),
        "preflight_revert": int(counters.get("preflight_revert", 0)),
        "preflight_slippage_fail": int(counters.get("preflight_slippage_fail", 0)),
        "drop_reason_counts": reasons.get("drop_reason_counts", {}),
        "viability_drop_reason_counts": reasons.get("viability_drop_reason_counts", {}),
    }
    http_eps = _settings_endpoints(settings, endpoints_attr="rpc_http_endpoints", fallback_urls_attr="rpc_urls")
    ws_eps = _settings_endpoints(settings, endpoints_attr="rpc_ws_endpoints", fallback_urls_attr="mempool_ws_urls")
    pairing = getattr(settings, "rpc_ws_pairing", None)
    if not pairing:
        pairing = _build_ws_http_pairing(ws_eps, http_eps)
    metrics_section.update({
        "rpc_http_endpoints_active": _endpoint_ids_from_settings(
            settings,
            endpoints_attr="rpc_http_endpoints",
            fallback_urls_attr="rpc_urls",
        ),
        "rpc_ws_endpoints_active": _endpoint_ids_from_settings(
            settings,
            endpoints_attr="rpc_ws_endpoints",
            fallback_urls_attr="mempool_ws_urls",
        ),
        "ws_http_pairing_active": pairing or {},
    })
    tx_fetch_attempts_total = int(counters.get("tx_fetch_attempts_total", counters.get("tx_fetch_total", 0)))
    tx_fetch_attempts_found = int(counters.get("tx_fetch_attempts_found", counters.get("tx_fetch_found", 0)))
    tx_fetch_unique_total = int(counters.get("tx_fetch_unique_total", 0))
    tx_fetch_unique_found = int(counters.get("tx_fetch_unique_found", 0))
    tx_fetch_attempt_rate = (
        float(tx_fetch_attempts_found) / float(tx_fetch_attempts_total)
        if tx_fetch_attempts_total > 0
        else None
    )
    tx_fetch_unique_rate = (
        float(tx_fetch_unique_found) / float(tx_fetch_unique_total)
        if tx_fetch_unique_total > 0
        else None
    )
    tx_fetch_batch = hist.get("tx_fetch_batch_size", {})
    metrics_section.update({
        "tx_fetch_attempts_total": tx_fetch_attempts_total,
        "tx_fetch_attempts_found": tx_fetch_attempts_found,
        "tx_fetch_unique_total": tx_fetch_unique_total,
        "tx_fetch_unique_found": tx_fetch_unique_found,
        "tx_fetch_not_found": int(counters.get("tx_fetch_not_found", 0)),
        "tx_fetch_timeout": int(counters.get("tx_fetch_timeout", 0)),
        "tx_fetch_rate_limited": int(counters.get("tx_fetch_rate_limited", 0)),
        "tx_fetch_errors_other": int(counters.get("tx_fetch_errors_other", 0)),
        "tx_fetch_retries_total": int(counters.get("tx_fetch_retries_total", 0)),
        "tx_fetch_attempt_success_rate": tx_fetch_attempt_rate,
        "tx_fetch_unique_success_rate": tx_fetch_unique_rate,
        "tx_fetch_success_rate": tx_fetch_unique_rate,
        "tx_fetch_batch_attempts": int(counters.get("tx_fetch_batch_attempts", 0)),
        "tx_fetch_batch_errors": int(counters.get("tx_fetch_batch_errors", 0)),
        "tx_fetch_single_calls_total": int(counters.get("tx_fetch_single_calls_total", 0)),
        "tx_fetch_batch_size_p50": tx_fetch_batch.get("p50"),
        "tx_fetch_batch_size_p95": tx_fetch_batch.get("p95"),
        "tx_fetch_queue_depth": _safe_int(mempool_status.get("tx_fetch_queue_depth")) if isinstance(mempool_status, dict) else None,
        "tx_fetch_batch_disabled_endpoints_count": _safe_int(mempool_status.get("tx_fetch_batch_disabled_endpoints_count")) if isinstance(mempool_status, dict) else None,
    })
    if isinstance(mempool_status, dict):
        if metrics_section["tx_fetch_attempts_total"] == 0:
            metrics_section["tx_fetch_attempts_total"] = _safe_int(mempool_status.get("tx_fetch_attempts_total")) or _safe_int(mempool_status.get("tx_fetch_total")) or 0
        if metrics_section["tx_fetch_attempts_found"] == 0:
            metrics_section["tx_fetch_attempts_found"] = _safe_int(mempool_status.get("tx_fetch_attempts_found")) or _safe_int(mempool_status.get("tx_fetch_found")) or 0
        if metrics_section["tx_fetch_unique_total"] == 0:
            metrics_section["tx_fetch_unique_total"] = _safe_int(mempool_status.get("tx_fetch_unique_total")) or 0
        if metrics_section["tx_fetch_unique_found"] == 0:
            metrics_section["tx_fetch_unique_found"] = _safe_int(mempool_status.get("tx_fetch_unique_found")) or 0
        if metrics_section["tx_fetch_not_found"] == 0:
            metrics_section["tx_fetch_not_found"] = _safe_int(mempool_status.get("tx_fetch_not_found")) or 0
        if metrics_section["tx_fetch_timeout"] == 0:
            metrics_section["tx_fetch_timeout"] = _safe_int(mempool_status.get("tx_fetch_timeout")) or 0
        if metrics_section["tx_fetch_rate_limited"] == 0:
            metrics_section["tx_fetch_rate_limited"] = _safe_int(mempool_status.get("tx_fetch_rate_limited")) or 0
        if metrics_section["tx_fetch_errors_other"] == 0:
            metrics_section["tx_fetch_errors_other"] = _safe_int(mempool_status.get("tx_fetch_errors_other")) or 0
        if metrics_section["tx_fetch_retries_total"] == 0:
            metrics_section["tx_fetch_retries_total"] = _safe_int(mempool_status.get("tx_fetch_retries_total")) or 0
        if metrics_section["tx_fetch_batch_attempts"] == 0:
            metrics_section["tx_fetch_batch_attempts"] = _safe_int(mempool_status.get("tx_fetch_batch_attempts")) or 0
        if metrics_section["tx_fetch_batch_errors"] == 0:
            metrics_section["tx_fetch_batch_errors"] = _safe_int(mempool_status.get("tx_fetch_batch_errors")) or 0
        if metrics_section["tx_fetch_single_calls_total"] == 0:
            metrics_section["tx_fetch_single_calls_total"] = _safe_int(mempool_status.get("tx_fetch_single_calls_total")) or 0
        if metrics_section["tx_fetch_unique_success_rate"] is None:
            metrics_section["tx_fetch_unique_success_rate"] = _safe_float(mempool_status.get("tx_fetch_unique_success_rate"))
        if metrics_section["tx_fetch_attempt_success_rate"] is None:
            metrics_section["tx_fetch_attempt_success_rate"] = _safe_float(mempool_status.get("tx_fetch_attempt_success_rate"))
        if metrics_section["tx_fetch_success_rate"] is None:
            metrics_section["tx_fetch_success_rate"] = _safe_float(mempool_status.get("tx_fetch_success_rate"))

    if rpc_health and isinstance(rpc_health, dict):
        endpoints = []
        for entry in rpc_health.get("endpoints", []) or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            endpoints.append({
                "url": _mask_url(url),
                "host": entry.get("host"),
                "success_rate": entry.get("success_rate"),
                "timeout_rate": entry.get("timeout_rate"),
                "p50_latency_ms": entry.get("p50_latency_ms"),
                "p95_latency_ms": entry.get("p95_latency_ms"),
                "banned_until": entry.get("banned_until"),
            })
        metrics_section.update({
            "rpc_endpoints_health": endpoints,
            "rpc_bans_total": rpc_health.get("bans_total"),
            "rpc_out_of_sync_bans_total": rpc_health.get("out_of_sync_bans_total"),
            "rpc_fallbacks_total": rpc_health.get("fallbacks_total"),
            "pinned_http_endpoint_last": _mask_url(rpc_health.get("pinned_last") or "") if rpc_health.get("pinned_last") else None,
        })

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
            "trigger_prepare_budget_ms": _safe_int(getattr(settings, "trigger_prepare_budget_ms", None)),
        },
        "simulation": {
            "slippage_bps": _safe_float(getattr(settings, "slippage_bps", None)),
            "mev_buffer_bps": _safe_float(getattr(settings, "mev_buffer_bps", None)),
            "min_profit_abs": _format_amount(getattr(settings, "min_profit_abs", None)),
            "min_profit_pct": _safe_float(getattr(settings, "min_profit_pct", None)),
            "sim_backend": str(getattr(settings, "sim_backend", "") or ""),
            "execution_mode": str(getattr(settings, "execution_mode", "") or ""),
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
            "ws_url_used": _mask_url(mempool_status.get("ws_url")) if isinstance(mempool_status, dict) and mempool_status.get("ws_url") else None,
        },
        "pipeline": pipeline,
        "last_trigger": last_trigger,
        "rolling": rolling,
        "config": config_snapshot,
        "metrics": metrics_section,
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
        rpc_health = None
        try:
            if self.rpc_provider and hasattr(self.rpc_provider, "health_snapshot"):
                rpc_health = self.rpc_provider.health_snapshot()
        except Exception:
            rpc_health = None
        snapshot = build_diagnostic_snapshot(
            log_dir=self.log_dir,
            settings=settings,
            session_started_at_s=self.session_started_at_s,
            current_block=self.get_current_block(),
            rpc_stats=self._rpc_stats(),
            rpc_health=rpc_health,
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
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                self.write_snapshot(reason="interval")
                await asyncio.sleep(float(self.interval_s))
            except Exception:
                await asyncio.sleep(float(self.interval_s))
