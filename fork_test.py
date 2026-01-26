# sim/fork_test.py

import asyncio
import json
import os
import math
import statistics
import sys
import time
import random
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from bot import scanner, strategies, config
from infra.rpc import RPCPool, set_pinned_http_endpoint, reset_pinned_http_endpoint
from infra.metrics import METRICS
from bot.simulator import simulate_candidate_async
from bot import preflight
from bot.block_context import BlockContext
from bot.routes import Hop
from bot.diagnostic import DiagnosticSnapshotter, build_diagnostic_snapshot
from bot.assistant_pack import write_assistant_pack
from execution.tx_pipeline import build_tx_ready
from ui_notify import ui_push
from mempool.engine import MempoolEngine
from mempool.types import Trigger


# RPC + scanner are initialized in main() after reading UI config
PS = None  # type: ignore[assignment]

SIM_FROM_ADDRESS = config.SIM_FROM_ADDRESS
MEMPOOL_BLOCK_CTX: Optional[BlockContext] = None


def _is_finite_number(x: Any) -> bool:
    try:
        return isinstance(x, (int, float)) and math.isfinite(float(x))
    except Exception:
        return False


def _clamp_sane(x: float, *, abs_max: float) -> float:
    # Defensive: prevent UI totals from exploding due to unit-mix bugs.
    # We keep a hard cap to catch accidental raw-unit mixes (e.g. 1e18),
    # but otherwise we preserve the value and let the UI format/round it.
    if not math.isfinite(x):
        return 0.0
    if abs(x) > abs_max:
        return float('nan')
    return float(x)


def _env_flag(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _normalize_drop_reason(reason: Optional[str]) -> str:
    if not reason:
        return "internal_error"
    r = str(reason).lower()
    if "budget" in r or "deadline" in r or "guard" in r or "remaining_budget" in r:
        return "time_budget_exhausted"
    if "no_base" in r or "no_cycle" in r or "no_route" in r or "no_candidates" in r:
        return "no_route"
    if "illiquid" in r or "viability" in r:
        return "illiquid"
    if "unknown_token" in r or "unknown_tokens" in r:
        return "unknown_token"
    if "gas" in r:
        return "gas_too_high"
    if "rpc" in r or "timeout" in r or "rate" in r or "http_" in r:
        return "quote_failed"
    if "quote" in r:
        return "quote_failed"
    if "slippage" in r:
        return "slippage_fail"
    if "profit" in r or "threshold" in r:
        return "min_profit_not_met"
    if "revert" in r:
        return "preflight_revert"
    return "internal_error"


LOG_DIR = Path(os.getenv("RUN_LOG_DIR", os.getenv("LOG_DIR", "logs")))
LOG_DIR.mkdir(exist_ok=True)
BLOCK_LOG = LOG_DIR / "blocks.jsonl"
HIT_LOG = LOG_DIR / "hits.jsonl"
SESSION_LOG = LOG_DIR / "last_session.md"


def _fmt_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _write_session_summary(
    *,
    started_at: datetime,
    updated_at: datetime,
    duration_s: float,
    rpc_urls: List[str],
    dexes: List[str],
    blocks_scanned: int,
    profit_hits: int,
    settings: "Settings",
    env_flags: Dict[str, str],
) -> None:
    try:
        lines: List[str] = []
        lines.append("# Last Session Summary")
        lines.append("")
        lines.append(f"- Started (UTC): {started_at.isoformat(timespec='seconds')}")
        lines.append(f"- Updated (UTC): {updated_at.isoformat(timespec='seconds')}")
        lines.append(f"- Duration: {_fmt_duration(duration_s)}")
        lines.append(f"- RPC endpoints: {len(rpc_urls)}")
        lines.append(f"- DEX adapters: {len(dexes)}")
        lines.append(f"- Blocks scanned: {blocks_scanned}")
        lines.append(f"- Profit hits: {profit_hits}")
        lines.append("")
        lines.append("## RPC URLs")
        if rpc_urls:
            for url in rpc_urls:
                lines.append(f"- {url}")
        else:
            lines.append("- (none)")
        lines.append("")
        lines.append("## DEX adapters")
        if dexes:
            for d in dexes:
                lines.append(f"- {d}")
        else:
            lines.append("- (none)")
        lines.append("")
        lines.append("## Runtime flags")
        for k, v in env_flags.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("## Settings")
        lines.append("```yaml")
        cfg = dict(settings.__dict__)
        for k in sorted(cfg.keys()):
            lines.append(f"{k}: {cfg[k]}")
        lines.append("```")
        SESSION_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


@dataclass
class Settings:
    # RPC (list for failover)
    rpc_urls: Tuple[str, ...] = ()
    rpc_http_endpoints: Tuple[Dict[str, Any], ...] = ()
    rpc_ws_endpoints: Tuple[Dict[str, Any], ...] = ()
    rpc_ws_pairing: Dict[str, str] = field(default_factory=dict)
    dexes: Tuple[str, ...] = ()
    enable_multidex: bool = False
    max_hops: int = 3
    beam_k: int = 20
    edge_top_m: int = 2
    trigger_prefer_cross_dex: bool = True
    trigger_require_cross_dex: bool = True
    trigger_require_three_hops: bool = True
    trigger_cross_dex_bonus_bps: float = 5.0
    trigger_same_dex_penalty_bps: float = 5.0
    trigger_edge_top_m_per_dex: int = 2
    trigger_base_fallback_enabled: bool = True
    trigger_allow_two_hop_fallback: bool = True
    trigger_cross_dex_fallback: bool = True
    trigger_connectors: Tuple[str, ...] = ()
    trigger_max_candidates_raw: int = 80
    trigger_prepare_budget_ms: int = 250
    probe_amount: float = 1.0

    # Mode
    scan_mode: str = "auto"  # auto|fixed
    scan_source: str = "block"  # block|mempool|hybrid

    # Thresholds
    min_profit_pct: float = 0.05  # percent of input (e.g. 0.05 == 0.05%)
    min_profit_abs: float = 0.05  # in base token units (usually USD if base is USDC/USDT)
    slippage_bps: float = 8.0
    mev_buffer_bps: float = 5.0
    max_gas_gwei: Optional[float] = None

    # Performance
    concurrency: int = 10
    block_budget_s: float = 10.0
    prepare_budget_ratio: float = 0.20
    prepare_budget_min_s: float = 2.0
    prepare_budget_max_s: float = 6.0
    expand_ratio_cap: float = 0.60
    expand_budget_max_s: float = 6.0
    min_scan_reserve_s: float = 0.6
    min_first_task_s: float = 0.08
    max_candidates_stage1: int = 200
    max_total_expanded: int = 400
    max_expanded_per_candidate: int = 6
    rpc_timeout_s: float = 3.0
    rpc_retry_count: int = 1
    rpc_health_ban_seconds: int = 60
    rpc_timeout_rate_threshold: float = 0.2
    rpc_latency_p95_ms_threshold: float = 2500.0
    out_of_sync_ban_seconds: int = 60
    tx_fetch_batch_enabled: bool = True
    tx_fetch_max_retries: int = 3
    tx_fetch_retry_backoff_ms: Tuple[int, ...] = (200, 500, 1000)
    tx_fetch_per_endpoint_max_inflight: int = 4

    # Fixed mode
    amount_presets: Tuple[float, ...] = (1.0, 5.0)

    # Auto mode
    stage1_amount: float = 1.0
    stage1_fee_tiers: Tuple[int, ...] = (500, 3000)
    stage2_top_k: int = 30
    stage2_amount_min: float = 0.5
    stage2_amount_max: float = 50.0
    stage2_max_evals: int = 6

    # RPC timeouts
    rpc_timeout_stage1_s: float = 6.0
    rpc_timeout_stage2_s: float = 10.0

    # V2 filters
    v2_min_reserve_ratio: float = 20.0
    v2_max_price_impact_bps: float = 300.0

    # Execution simulation
    sim_backend: str = "quote"  # quote | eth_call | state_override
    arb_executor_address: str = ""
    arb_executor_owner: str = ""
    execution_mode: str = "off"  # off | dryrun

    # Reporting / base currency (UI should stay consistent)
    # Profits, spent, ROI are computed and displayed in this token.
    report_currency: str = "USDC"  # USDC|USDT

    # Mempool settings
    mempool_enabled: bool = False
    mempool_ws_urls: Tuple[str, ...] = ()
    mempool_max_inflight_tx: int = 200
    mempool_fetch_tx_concurrency: int = 20
    mempool_filter_to: Tuple[str, ...] = ()
    mempool_watch_mode: str = "strict"
    mempool_watched_router_sets: str = "core"
    mempool_min_value_usd: float = 25.0
    mempool_usd_per_eth: float = 2000.0
    mempool_allow_unknown_tokens: bool = True
    mempool_raw_min_enabled: bool = False
    mempool_strict_unknown_tokens: bool = False
    mempool_dedup_ttl_s: int = 120
    mempool_trigger_scan_budget_s: float = 1.5
    mempool_trigger_max_queue: int = 50
    mempool_trigger_max_concurrent: int = 1
    mempool_trigger_ttl_s: int = 60
    mempool_confirm_timeout_s: float = 2.0
    mempool_post_scan_budget_s: float = 1.0


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


def _normalize_endpoint_list(raw: Any) -> List[Dict[str, str]]:
    endpoints: List[Dict[str, str]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                eid = str(item.get("id") or "").strip() or _endpoint_id_from_url(url)
                endpoints.append({"id": eid, "url": url})
            elif isinstance(item, str):
                url = item.strip()
                if url:
                    endpoints.append({"id": _endpoint_id_from_url(url), "url": url})
    return endpoints


def _build_ws_http_pairing(ws_endpoints: List[Dict[str, str]], http_endpoints: List[Dict[str, str]]) -> Dict[str, str]:
    pairing: Dict[str, str] = {}
    http_by_id: Dict[str, str] = {}
    http_by_host: Dict[str, str] = {}
    for ep in http_endpoints:
        if not isinstance(ep, dict):
            continue
        eid = str(ep.get("id") or "").strip()
        url = str(ep.get("url") or "").strip()
        if eid and url:
            http_by_id[eid] = url
        if url:
            host = url.split("://", 1)[-1].split("/", 1)[0].lower()
            http_by_host[host] = url
    for ep in ws_endpoints:
        if not isinstance(ep, dict):
            continue
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


def _load_settings() -> Settings:
    path = os.getenv("BOT_CONFIG", "bot_config.json")
    if not os.path.exists(path):
        return Settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return Settings()

    s = Settings()
    for k, v in raw.items():
        if not hasattr(s, k):
            continue
        try:
            setattr(s, k, v)
        except Exception:
            pass
    try:
        http_eps = _normalize_endpoint_list(raw.get("rpc_http_endpoints"))
        if http_eps:
            s.rpc_http_endpoints = tuple(http_eps)
            s.rpc_urls = tuple(ep.get("url") for ep in http_eps if ep.get("url"))
    except Exception:
        pass
    try:
        ws_eps = _normalize_endpoint_list(raw.get("rpc_ws_endpoints"))
        if ws_eps:
            s.rpc_ws_endpoints = tuple(ws_eps)
            s.mempool_ws_urls = tuple(ep.get("url") for ep in ws_eps if ep.get("url"))
    except Exception:
        pass
    try:
        pairing = raw.get("rpc_ws_pairing")
        if isinstance(pairing, dict):
            s.rpc_ws_pairing = {str(k): str(v) for k, v in pairing.items() if k and v}
    except Exception:
        pass
    # normalize tuples
    try:
        ru = raw.get("rpc_http_urls") if raw.get("rpc_http_urls") is not None else raw.get("rpc_urls")
        if isinstance(ru, (list, tuple)):
            s.rpc_urls = tuple(str(x).strip() for x in ru if str(x).strip())
        elif isinstance(ru, str):
            s.rpc_urls = tuple(x.strip() for x in ru.split(",") if x.strip())
    except Exception:
        pass
    try:
        ws_raw = raw.get("rpc_ws_urls")
        if ws_raw is None:
            ws_raw = raw.get("mempool_ws_urls")
        if isinstance(ws_raw, (list, tuple)):
            s.mempool_ws_urls = tuple(str(x).strip() for x in ws_raw if str(x).strip())
        elif isinstance(ws_raw, str):
            s.mempool_ws_urls = tuple(x.strip() for x in ws_raw.replace("\n", ",").split(",") if x.strip())
    except Exception:
        pass
    if not s.rpc_http_endpoints and s.rpc_urls:
        s.rpc_http_endpoints = tuple(_normalize_endpoint_list(list(s.rpc_urls)))
    if not s.rpc_ws_endpoints and s.mempool_ws_urls:
        s.rpc_ws_endpoints = tuple(_normalize_endpoint_list(list(s.mempool_ws_urls)))
    if not s.rpc_ws_pairing:
        s.rpc_ws_pairing = _build_ws_http_pairing(list(s.rpc_ws_endpoints), list(s.rpc_http_endpoints))
    try:
        s.amount_presets = tuple(float(x) for x in (raw.get("amount_presets") or s.amount_presets))
    except Exception:
        pass
    try:
        s.stage1_fee_tiers = tuple(int(x) for x in (raw.get("stage1_fee_tiers") or s.stage1_fee_tiers))
    except Exception:
        pass
    try:
        dx = raw.get("dexes")
        if isinstance(dx, (list, tuple)):
            s.dexes = tuple(str(x).strip().lower() for x in dx if str(x).strip())
        elif isinstance(dx, str):
            s.dexes = tuple(x.strip().lower() for x in dx.replace("\n", ",").split(",") if x.strip())
    except Exception:
        pass
    try:
        wm = raw.get("mempool_watch_mode", s.mempool_watch_mode)
        s.mempool_watch_mode = str(wm).strip().lower()
    except Exception:
        pass
    try:
        ws = raw.get("mempool_watched_router_sets", s.mempool_watched_router_sets)
        s.mempool_watched_router_sets = str(ws).strip().lower()
    except Exception:
        pass
    try:
        val = raw.get("mempool_allow_unknown_tokens", s.mempool_allow_unknown_tokens)
        if isinstance(val, str):
            s.mempool_allow_unknown_tokens = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.mempool_allow_unknown_tokens = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("mempool_raw_min_enabled", s.mempool_raw_min_enabled)
        if isinstance(val, str):
            s.mempool_raw_min_enabled = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.mempool_raw_min_enabled = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("tx_fetch_batch_enabled", s.tx_fetch_batch_enabled)
        if isinstance(val, str):
            s.tx_fetch_batch_enabled = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.tx_fetch_batch_enabled = bool(val)
    except Exception:
        pass
    try:
        s.tx_fetch_max_retries = int(raw.get("tx_fetch_max_retries", s.tx_fetch_max_retries))
    except Exception:
        pass
    try:
        backoff = raw.get("tx_fetch_retry_backoff_ms", s.tx_fetch_retry_backoff_ms)
        if isinstance(backoff, (list, tuple)):
            s.tx_fetch_retry_backoff_ms = tuple(int(x) for x in backoff if int(x) >= 0)
        elif isinstance(backoff, str):
            s.tx_fetch_retry_backoff_ms = tuple(
                int(x) for x in backoff.replace("\n", ",").split(",") if x.strip().isdigit()
            )
    except Exception:
        pass
    try:
        s.tx_fetch_per_endpoint_max_inflight = int(
            raw.get("tx_fetch_per_endpoint_max_inflight", s.tx_fetch_per_endpoint_max_inflight)
        )
    except Exception:
        pass
    try:
        s.rpc_health_ban_seconds = int(raw.get("rpc_health_ban_seconds", s.rpc_health_ban_seconds))
    except Exception:
        pass
    try:
        s.rpc_timeout_rate_threshold = float(raw.get("rpc_timeout_rate_threshold", s.rpc_timeout_rate_threshold))
    except Exception:
        pass
    try:
        s.rpc_latency_p95_ms_threshold = float(raw.get("rpc_latency_p95_ms_threshold", s.rpc_latency_p95_ms_threshold))
    except Exception:
        pass
    try:
        s.out_of_sync_ban_seconds = int(raw.get("out_of_sync_ban_seconds", s.out_of_sync_ban_seconds))
    except Exception:
        pass
    try:
        sb = raw.get("sim_backend", s.sim_backend)
        s.sim_backend = str(sb).strip().lower()
    except Exception:
        pass
    try:
        em = raw.get("execution_mode", s.execution_mode)
        s.execution_mode = str(em).strip().lower()
    except Exception:
        pass
    try:
        addr = raw.get("arb_executor_address", s.arb_executor_address)
        s.arb_executor_address = str(addr).strip()
    except Exception:
        pass
    try:
        addr = raw.get("arb_executor_owner", s.arb_executor_owner)
        s.arb_executor_owner = str(addr).strip()
    except Exception:
        pass

    try:
        emd = raw.get("enable_multidex", s.enable_multidex)
        if isinstance(emd, str):
            s.enable_multidex = emd.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.enable_multidex = bool(emd)
    except Exception:
        s.enable_multidex = False
    try:
        val = raw.get("trigger_prefer_cross_dex", s.trigger_prefer_cross_dex)
        if isinstance(val, str):
            s.trigger_prefer_cross_dex = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_prefer_cross_dex = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("trigger_require_cross_dex", s.trigger_require_cross_dex)
        if isinstance(val, str):
            s.trigger_require_cross_dex = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_require_cross_dex = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("trigger_require_three_hops", s.trigger_require_three_hops)
        if isinstance(val, str):
            s.trigger_require_three_hops = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_require_three_hops = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("trigger_base_fallback_enabled", s.trigger_base_fallback_enabled)
        if isinstance(val, str):
            s.trigger_base_fallback_enabled = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_base_fallback_enabled = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("trigger_allow_two_hop_fallback", s.trigger_allow_two_hop_fallback)
        if isinstance(val, str):
            s.trigger_allow_two_hop_fallback = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_allow_two_hop_fallback = bool(val)
    except Exception:
        pass
    try:
        val = raw.get("trigger_cross_dex_fallback", s.trigger_cross_dex_fallback)
        if isinstance(val, str):
            s.trigger_cross_dex_fallback = val.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.trigger_cross_dex_fallback = bool(val)
    except Exception:
        pass
    try:
        raw_connectors = raw.get("trigger_connectors")
        if isinstance(raw_connectors, (list, tuple)):
            s.trigger_connectors = tuple(str(x).strip() for x in raw_connectors if str(x).strip())
        elif isinstance(raw_connectors, str):
            parts = [p.strip() for p in raw_connectors.replace("\n", ",").split(",") if p.strip()]
            s.trigger_connectors = tuple(parts)
    except Exception:
        pass
    try:
        s.trigger_max_candidates_raw = int(raw.get("trigger_max_candidates_raw", s.trigger_max_candidates_raw))
    except Exception:
        pass
    try:
        src = str(raw.get("scan_source", s.scan_source)).strip().lower()
        if src in ("block", "mempool", "hybrid"):
            s.scan_source = src
    except Exception:
        s.scan_source = "block"
    try:
        s.max_hops = int(raw.get("max_hops", s.max_hops))
    except Exception:
        pass
    try:
        s.beam_k = int(raw.get("beam_k", s.beam_k))
    except Exception:
        pass
    try:
        s.edge_top_m = int(raw.get("edge_top_m", s.edge_top_m))
    except Exception:
        pass
    try:
        s.trigger_edge_top_m_per_dex = int(raw.get("trigger_edge_top_m_per_dex", s.trigger_edge_top_m_per_dex))
    except Exception:
        pass
    try:
        s.probe_amount = float(raw.get("probe_amount", s.probe_amount))
    except Exception:
        pass

    # normalize report currency
    try:
        rc = raw.get("report_currency", s.report_currency)
        rc = str(rc).strip().upper()
        if rc not in ("USDC", "USDT"):
            rc = "USDC"
        s.report_currency = rc
    except Exception:
        s.report_currency = "USDC"
    # numeric fields
    try:
        s.mev_buffer_bps = float(raw.get("mev_buffer_bps", s.mev_buffer_bps))
    except Exception:
        pass
    try:
        s.trigger_cross_dex_bonus_bps = float(raw.get("trigger_cross_dex_bonus_bps", s.trigger_cross_dex_bonus_bps))
    except Exception:
        pass
    try:
        s.trigger_same_dex_penalty_bps = float(raw.get("trigger_same_dex_penalty_bps", s.trigger_same_dex_penalty_bps))
    except Exception:
        pass
    try:
        s.concurrency = int(raw.get("concurrency", s.concurrency))
    except Exception:
        pass
    try:
        s.block_budget_s = float(raw.get("block_budget_s", s.block_budget_s))
    except Exception:
        pass
    try:
        s.prepare_budget_ratio = float(raw.get("prepare_budget_ratio", s.prepare_budget_ratio))
    except Exception:
        pass
    try:
        s.prepare_budget_min_s = float(raw.get("prepare_budget_min_s", s.prepare_budget_min_s))
    except Exception:
        pass
    try:
        s.prepare_budget_max_s = float(raw.get("prepare_budget_max_s", s.prepare_budget_max_s))
    except Exception:
        pass
    try:
        s.expand_ratio_cap = float(raw.get("expand_ratio_cap", s.expand_ratio_cap))
    except Exception:
        pass
    try:
        s.expand_budget_max_s = float(raw.get("expand_budget_max_s", s.expand_budget_max_s))
    except Exception:
        pass
    try:
        s.min_scan_reserve_s = float(raw.get("min_scan_reserve_s", s.min_scan_reserve_s))
    except Exception:
        pass
    try:
        s.min_first_task_s = float(raw.get("min_first_task_s", s.min_first_task_s))
    except Exception:
        pass
    try:
        s.max_candidates_stage1 = int(raw.get("max_candidates_stage1", s.max_candidates_stage1))
    except Exception:
        pass
    try:
        s.max_total_expanded = int(raw.get("max_total_expanded", s.max_total_expanded))
    except Exception:
        pass
    try:
        s.max_expanded_per_candidate = int(raw.get("max_expanded_per_candidate", s.max_expanded_per_candidate))
    except Exception:
        pass
    try:
        s.rpc_timeout_s = float(raw.get("rpc_timeout_s", s.rpc_timeout_s))
    except Exception:
        pass
    try:
        s.rpc_retry_count = int(raw.get("rpc_retry_count", s.rpc_retry_count))
    except Exception:
        pass
    try:
        me = raw.get("mempool_enabled", s.mempool_enabled)
        if isinstance(me, str):
            s.mempool_enabled = me.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.mempool_enabled = bool(me)
    except Exception:
        s.mempool_enabled = False
    try:
        ws_urls = raw.get("mempool_ws_urls")
        if isinstance(ws_urls, (list, tuple)):
            s.mempool_ws_urls = tuple(str(x).strip() for x in ws_urls if str(x).strip())
        elif isinstance(ws_urls, str):
            s.mempool_ws_urls = tuple(x.strip() for x in ws_urls.replace("\\n", ",").split(",") if x.strip())
    except Exception:
        pass
    try:
        flt = raw.get("mempool_filter_to")
        if isinstance(flt, (list, tuple)):
            s.mempool_filter_to = tuple(str(x).strip().lower() for x in flt if str(x).strip())
        elif isinstance(flt, str):
            s.mempool_filter_to = tuple(x.strip().lower() for x in flt.replace("\\n", ",").split(",") if x.strip())
    except Exception:
        pass
    try:
        s.mempool_max_inflight_tx = int(raw.get("mempool_max_inflight_tx", s.mempool_max_inflight_tx))
    except Exception:
        pass
    try:
        s.mempool_fetch_tx_concurrency = int(raw.get("mempool_fetch_tx_concurrency", s.mempool_fetch_tx_concurrency))
    except Exception:
        pass
    try:
        s.mempool_min_value_usd = float(raw.get("mempool_min_value_usd", s.mempool_min_value_usd))
    except Exception:
        pass
    try:
        s.mempool_usd_per_eth = float(raw.get("mempool_usd_per_eth", s.mempool_usd_per_eth))
    except Exception:
        pass
    try:
        s.mempool_dedup_ttl_s = int(raw.get("mempool_dedup_ttl_s", s.mempool_dedup_ttl_s))
    except Exception:
        pass
    try:
        s.mempool_trigger_scan_budget_s = float(raw.get("mempool_trigger_scan_budget_s", s.mempool_trigger_scan_budget_s))
    except Exception:
        pass
    try:
        s.mempool_trigger_max_queue = int(raw.get("mempool_trigger_max_queue", s.mempool_trigger_max_queue))
    except Exception:
        pass
    try:
        s.mempool_trigger_max_concurrent = int(raw.get("mempool_trigger_max_concurrent", s.mempool_trigger_max_concurrent))
    except Exception:
        pass
    try:
        s.mempool_trigger_ttl_s = int(raw.get("mempool_trigger_ttl_s", s.mempool_trigger_ttl_s))
    except Exception:
        pass
    try:
        s.mempool_confirm_timeout_s = float(raw.get("mempool_confirm_timeout_s", s.mempool_confirm_timeout_s))
    except Exception:
        pass
    try:
        s.mempool_post_scan_budget_s = float(raw.get("mempool_post_scan_budget_s", s.mempool_post_scan_budget_s))
    except Exception:
        pass
    try:
        s.v2_min_reserve_ratio = float(raw.get("v2_min_reserve_ratio", s.v2_min_reserve_ratio))
    except Exception:
        pass
    try:
        s.v2_max_price_impact_bps = float(raw.get("v2_max_price_impact_bps", s.v2_max_price_impact_bps))
    except Exception:
        pass

    # Optional debug profile for "see some profits" diagnostics.
    profile = str(os.getenv("SIM_PROFILE", "")).strip().lower()
    if profile == "debug":
        rc = str(getattr(s, "report_currency", "USDC") or "USDC").upper()
        if rc in ("USDC", "USDT", "DAI"):
            s.stage1_amount = 2000.0
        s.min_profit_abs = 2.0
        s.min_profit_pct = 0.01  # 0.01%
        s.slippage_bps = 5.0
    return s


def _decimals_by_token(addr: str) -> int:
    return int(config.token_decimals(addr))


def _scale_amount(token_in: str, amount_units: Any) -> int:
    dec = _decimals_by_token(token_in)
    try:
        amt = Decimal(str(amount_units))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    scale = Decimal(10) ** int(dec)
    try:
        return int((amt * scale).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError):
        return 0


def _route_pretty(
    route: Tuple[str, ...],
    route_dex: Optional[Tuple[str, ...]] = None,
    route_fee_bps: Optional[Tuple[int, ...]] = None,
    route_fee_tier: Optional[Tuple[int, ...]] = None,
    dex_path: Optional[Tuple[str, ...]] = None,
) -> str:
    parts: List[str] = []
    for i, addr in enumerate(route):
        parts.append(config.token_symbol(addr))
        if dex_path and i < len(dex_path):
            parts.append(f"-[{dex_path[i]}]->")
        elif route_dex and i < len(route_dex):
            dex = str(route_dex[i])
            fee_bps = None
            fee_tier = None
            if route_fee_bps and i < len(route_fee_bps):
                fee_bps = route_fee_bps[i]
            if route_fee_tier and i < len(route_fee_tier):
                fee_tier = route_fee_tier[i]
            if fee_tier:
                parts.append(f"-[{dex}:{int(fee_tier)}]->")
            elif fee_bps is not None:
                fee_pct = float(fee_bps) / 100.0
                parts.append(f"-[{dex} {fee_pct:.2f}%]->")
            else:
                parts.append(f"-[{dex}]->")
    return " ".join(parts)


async def _confirm_mempool(mempool_engine: Optional[MempoolEngine], block_number: int) -> None:
    if mempool_engine:
        await mempool_engine.confirm_block(int(block_number))


async def wait_for_new_block(last_block: int, *, timeout_s: float = 3.0) -> int:
    while True:
        try:
            current_block = await PS.rpc.get_block_number(timeout_s=timeout_s)
        except Exception:
            await asyncio.sleep(0.25)
            continue
        if int(current_block) > int(last_block):
            return int(current_block)
        await asyncio.sleep(0.25)


async def scan_routes() -> List[Tuple[str, ...]]:
    s = _load_settings()
    rc = str(getattr(s, "report_currency", "USDC") or "USDC").upper()
    # Ensure we always scan cycles that start/end in the reporting currency,
    # so profit/spent symbols in the UI cannot drift.
    base_addr = config.TOKENS.get(rc, config.TOKENS["USDC"])
    return strategies.Strategy(bases=[base_addr]).get_routes(max_hops=int(getattr(s, "max_hops", 3)))


def _build_trigger_routes(
    tokens_involved: List[str],
    *,
    max_hops: int,
    token_universe: Optional[List[str]] = None,
    require_three_hops: bool = False,
    base_token: Optional[str] = None,
    connectors: Optional[List[str]] = None,
) -> List[Tuple[str, ...]]:
    if base_token:
        try:
            base_addr = config.token_address(str(base_token))
        except Exception:
            base_addr = str(base_token)
    else:
        s = _load_settings()
        rc = str(getattr(s, "report_currency", "USDC") or "USDC").upper()
        base_addr = config.TOKENS.get(rc, config.TOKENS["USDC"])
    if not base_addr:
        return []
    base_norm = str(base_addr).lower()
    if connectors is None:
        connectors = list(getattr(config, "MEMPOOL_TRIGGER_CONNECTORS", [])) or [
            config.TOKENS.get("WETH"),
            config.TOKENS.get("USDC"),
            config.TOKENS.get("USDT"),
            config.TOKENS.get("DAI"),
        ]
    token_source = token_universe or tokens_involved
    tokens: List[str] = []
    seen: set[str] = set()
    for t in list(token_source) + connectors:
        if not t:
            continue
        try:
            addr = config.token_address(str(t))
        except Exception:
            addr = str(t)
        addr = str(addr).lower()
        if addr == base_norm:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        tokens.append(addr)
    if not tokens:
        return []
    if max_hops < 2:
        max_hops = 2
    if max_hops > 4:
        max_hops = 4
    if require_three_hops and max_hops < 3:
        max_hops = 3
    # Limit tokens to keep trigger scan fast.
    tokens = tokens[:8]

    routes: List[Tuple[str, ...]] = []
    if max_hops >= 2 and not require_three_hops:
        for x in tokens:
            if x != base_norm:
                routes.append((base_norm, x, base_norm))
    if max_hops >= 3:
        for a in tokens:
            for b in tokens:
                if a == b:
                    continue
                routes.append((base_norm, a, b, base_norm))
    if max_hops >= 4:
        mids = tokens[:6]
        for a in mids:
            for b in mids:
                if a == b:
                    continue
                for c in mids:
                    if c == a or c == b:
                        continue
                    routes.append((base_norm, a, b, c, base_norm))

    uniq: List[Tuple[str, ...]] = []
    seen = set()
    for r in routes:
        if r in seen:
            continue
        seen.add(r)
        uniq.append(r)
    return uniq


def _normalize_token_list(tokens: List[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for t in tokens:
        if not t:
            continue
        try:
            addr = config.token_address(str(t))
        except Exception:
            addr = str(t)
        addr = str(addr).lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


def _resolve_base_candidates(primary_symbol: str, *, fallback_enabled: bool) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    if primary_symbol:
        try:
            addr = config.token_address(primary_symbol)
        except Exception:
            addr = primary_symbol
        if addr:
            candidates.append((primary_symbol, str(addr).lower()))
    if fallback_enabled:
        for t in list(getattr(config, "MEMPOOL_TRIGGER_BASE_FALLBACK", [])):
            if not t:
                continue
            try:
                addr = config.token_address(str(t))
            except Exception:
                addr = str(t)
            if not addr:
                continue
            addr = str(addr).lower()
            sym = config.token_symbol(addr)
            if any(addr == existing[1] for existing in candidates):
                continue
            candidates.append((sym, addr))
    return candidates


async def _scan_trigger(trigger: Trigger, budget_s: float) -> Dict[str, Any]:
    s = _load_settings()
    if not PS or MEMPOOL_BLOCK_CTX is None:
        result = {
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_context",
            "candidates_raw": None,
            "candidates_after_prepare": None,
            "candidates_after_universe": None,
            "candidates_after_base_filter": None,
            "candidates_after_hops_filter": None,
            "candidates_after_cross_dex_filter": None,
            "candidates_after_viability_filter": None,
            "candidates_after_caps": None,
            "candidates_scheduled": 0,
            "zero_candidates_reason": "rpc_unavailable",
            "zero_candidates_detail": "no_context",
            "zero_candidates_stage": "schedule",
            "zero_schedule_reason": "rpc_unavailable",
            "zero_schedule_detail": "no_context",
            "base_used": None,
            "base_fallback_used": False,
            "hops_attempted": [],
            "hop_fallback_used": False,
            "connectors_added": [],
            "cross_dex_fallback_used": False,
            "prepare_truncated": False,
            "prepare_time_ms": 0.0,
            "schedule_guard_triggered": False,
            "budget_remaining_ms_at_schedule": None,
        }
        METRICS.inc_reason("drop_reason_counts", "rpc_unavailable", 1)
        return result
    block_ctx = MEMPOOL_BLOCK_CTX
    set_pinned_http_endpoint(getattr(block_ctx, "pinned_http_endpoint", None))
    loop = asyncio.get_running_loop()
    prepare_start_t = loop.time()
    prepare_budget_ms = int(
        getattr(s, "trigger_prepare_budget_ms", getattr(config, "TRIGGER_PREPARE_BUDGET_MS", 250))
    )
    if prepare_budget_ms < 0:
        prepare_budget_ms = 0
    prepare_deadline = prepare_start_t + (float(prepare_budget_ms) / 1000.0)
    scan_deadline = prepare_start_t + (float(prepare_budget_ms) / 1000.0) + float(budget_s)

    def _elapsed_prepare_ms() -> float:
        return max(0.0, (loop.time() - prepare_start_t) * 1000.0)
    require_three_hops = bool(getattr(s, "trigger_require_three_hops", getattr(config, "TRIGGER_REQUIRE_THREE_HOPS", True)))
    allow_two_hop_fallback = bool(
        getattr(s, "trigger_allow_two_hop_fallback", getattr(config, "TRIGGER_ALLOW_TWO_HOP_FALLBACK", True))
    )
    base_fallback_enabled = bool(
        getattr(s, "trigger_base_fallback_enabled", getattr(config, "TRIGGER_BASE_FALLBACK_ENABLED", True))
    )
    cross_dex_fallback = bool(
        getattr(s, "trigger_cross_dex_fallback", getattr(config, "TRIGGER_CROSS_DEX_FALLBACK", True))
    )
    max_hops = int(getattr(s, "max_hops", 3))
    trigger_max_candidates_raw = int(
        getattr(s, "trigger_max_candidates_raw", getattr(config, "TRIGGER_MAX_CANDIDATES_RAW", 80))
    )

    raw_tokens = trigger.token_universe or trigger.tokens_involved or []
    tokens_norm = _normalize_token_list(list(raw_tokens))
    connectors_cfg = list(getattr(s, "trigger_connectors", ()) or []) or list(
        getattr(config, "MEMPOOL_TRIGGER_CONNECTORS", [])
    )
    connectors_norm = _normalize_token_list(connectors_cfg)
    extras_norm = _normalize_token_list(list(getattr(config, "MEMPOOL_TRIGGER_EXTRA_TOKENS", [])))

    weth_addr = config.TOKENS.get("WETH")
    usdt_addr = config.TOKENS.get("USDT")
    if weth_addr and usdt_addr:
        weth_norm = str(config.token_address(weth_addr)).lower()
        usdt_norm = str(config.token_address(usdt_addr)).lower()
        if weth_norm in set(tokens_norm + connectors_norm) and usdt_norm not in connectors_norm:
            connectors_norm.append(usdt_norm)

    token_universe: List[str] = []
    for t in tokens_norm + connectors_norm + extras_norm:
        if t and t not in token_universe:
            token_universe.append(t)

    connectors_added: List[str] = []
    for t in connectors_norm + extras_norm:
        if t and t not in tokens_norm and t not in connectors_added:
            connectors_added.append(t)

    base_used: Optional[str] = None
    base_fallback_used = False
    hop_fallback_used = False
    hops_attempted: List[int] = []
    routes: List[Tuple[str, ...]] = []
    candidates_raw: Optional[int] = None
    candidates_after_universe: Optional[int] = None
    prepare_truncated = False
    prepare_time_ms: Optional[float] = None

    rc = str(getattr(s, "report_currency", "USDC") or "USDC").upper()
    base_candidates = _resolve_base_candidates(rc, fallback_enabled=base_fallback_enabled)
    def _finalize(res: Dict[str, Any]) -> Dict[str, Any]:
        try:
            cr = res.get("candidates_raw")
            if cr is not None:
                METRICS.inc("candidates_generated", int(cr))
            probe = res.get("candidates_after_viability_filter")
            if probe is None:
                probe = res.get("candidates_after_caps")
            if probe is not None:
                METRICS.inc("candidates_after_probe", int(probe))
            refined = res.get("candidates_after_cross_dex_filter")
            if refined is None:
                refined = res.get("candidates_after_caps")
            if refined is not None:
                METRICS.inc("candidates_after_refine", int(refined))
            if int(res.get("scheduled", 0) or 0) == 0:
                reason = res.get("zero_candidates_reason") or res.get("zero_schedule_reason")
                METRICS.inc_reason("drop_reason_counts", _normalize_drop_reason(reason), 1)
        except Exception:
            pass
        return res

    if not base_candidates:
        return _finalize({
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_base",
            "candidates_raw": None,
            "candidates_after_prepare": None,
            "candidates_after_universe": None,
            "candidates_after_base_filter": None,
            "candidates_after_hops_filter": None,
            "candidates_after_cross_dex_filter": None,
            "candidates_after_viability_filter": None,
            "candidates_after_caps": None,
            "candidates_scheduled": 0,
            "zero_candidates_reason": "no_base_currency_in_universe",
            "zero_candidates_detail": "no_base_candidates",
            "zero_candidates_stage": "base",
            "zero_schedule_reason": "no_base_currency_in_universe",
            "zero_schedule_detail": "no_base_candidates",
            "base_used": None,
            "base_fallback_used": False,
            "hops_attempted": [],
            "hop_fallback_used": False,
            "connectors_added": connectors_added,
            "cross_dex_fallback_used": False,
            "prepare_truncated": False,
            "prepare_time_ms": float(_elapsed_prepare_ms()),
            "schedule_guard_triggered": False,
            "budget_remaining_ms_at_schedule": None,
        })

    for idx, (base_sym, base_addr) in enumerate(base_candidates):
        base_sym = str(base_sym or "").upper()
        base_addr = str(base_addr or "").lower()
        if not base_addr:
            continue

        routes_primary: List[Tuple[str, ...]] = []
        hops_attempted_local: List[int] = []
        want_three = max_hops >= 3
        if want_three:
            hops_attempted_local.append(3)
            routes_primary = _build_trigger_routes(
                trigger.tokens_involved,
                max_hops=3,
                token_universe=token_universe,
                require_three_hops=True,
                base_token=base_addr,
                connectors=connectors_norm,
            )
            if candidates_raw is None and idx == 0:
                candidates_raw = len(routes_primary)
        elif max_hops >= 2:
            hops_attempted_local.append(2)
            routes_primary = _build_trigger_routes(
                trigger.tokens_involved,
                max_hops=2,
                token_universe=token_universe,
                require_three_hops=False,
                base_token=base_addr,
                connectors=connectors_norm,
            )
            if candidates_raw is None and idx == 0:
                candidates_raw = len(routes_primary)

        routes_use = list(routes_primary)
        hop_fallback_used_local = False
        if not routes_use and allow_two_hop_fallback and want_three and max_hops >= 2:
            routes_use = _build_trigger_routes(
                trigger.tokens_involved,
                max_hops=2,
                token_universe=token_universe,
                require_three_hops=False,
                base_token=base_addr,
                connectors=connectors_norm,
            )
            hop_fallback_used_local = True
            hops_attempted_local.append(2)

        if routes_use:
            routes = routes_use
            base_used = base_sym or config.token_symbol(base_addr)
            base_fallback_used = idx > 0
            hop_fallback_used = hop_fallback_used_local
            hops_attempted = hops_attempted_local
            break
        if loop.time() >= prepare_deadline:
            prepare_truncated = True
            break

    if candidates_raw is None:
        candidates_raw = 0

    if not routes:
        detail = "no_tokens" if not token_universe else "no_routes_for_base"
        return _finalize({
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_routes",
            "candidates_raw": int(candidates_raw),
            "candidates_after_prepare": 0,
            "candidates_after_universe": 0,
            "candidates_after_base_filter": 0,
            "candidates_after_hops_filter": 0,
            "candidates_after_cross_dex_filter": 0,
            "candidates_after_viability_filter": None,
            "candidates_after_caps": 0,
            "candidates_scheduled": 0,
            "zero_candidates_reason": "no_cycles_generated",
            "zero_candidates_detail": detail,
            "zero_candidates_stage": "hops",
            "zero_schedule_reason": "no_candidates",
            "zero_schedule_detail": detail,
            "base_used": base_used,
            "base_fallback_used": base_fallback_used,
            "hops_attempted": hops_attempted,
            "hop_fallback_used": hop_fallback_used,
            "connectors_added": connectors_added,
            "cross_dex_fallback_used": False,
            "prepare_truncated": bool(prepare_truncated),
            "prepare_time_ms": float(_elapsed_prepare_ms()),
            "schedule_guard_triggered": False,
            "budget_remaining_ms_at_schedule": None,
        })

    candidates_after_base_filter = len(routes)
    candidates_after_hops_filter = len(routes)
    candidates_after_universe = len(routes)

    capped_by_trigger = False
    if trigger_max_candidates_raw > 0 and len(routes) > int(trigger_max_candidates_raw):
        routes = routes[: int(trigger_max_candidates_raw)]
        capped_by_trigger = True

    prepare_time_ms = max(0.0, (loop.time() - prepare_start_t) * 1000.0)
    if loop.time() >= prepare_deadline:
        prepare_truncated = True

    timeout_s = max(0.2, min(float(s.rpc_timeout_stage1_s), float(budget_s)))

    base_addr = config.TOKENS.get(str(base_used or rc).upper(), config.TOKENS["USDC"])
    amount_in = _scale_amount(base_addr, s.stage1_amount)
    candidates = [
        {"route": route, "amount_in": int(amount_in), "trigger_tx_hash": str(trigger.tx_hash)}
        for route in routes
    ]
    candidates_after_prepare = len(candidates)

    prefer_cross_dex = bool(getattr(s, "trigger_prefer_cross_dex", getattr(config, "TRIGGER_PREFER_CROSS_DEX", True)))
    require_cross_dex = bool(getattr(s, "trigger_require_cross_dex", getattr(config, "TRIGGER_REQUIRE_CROSS_DEX", True)))
    cross_bonus = float(getattr(s, "trigger_cross_dex_bonus_bps", getattr(config, "TRIGGER_CROSS_DEX_BONUS_BPS", 0.0)))
    same_penalty = float(getattr(s, "trigger_same_dex_penalty_bps", getattr(config, "TRIGGER_SAME_DEX_PENALTY_BPS", 0.0)))
    if not getattr(PS, "dexes", None) or len(getattr(PS, "dexes", [])) < 2:
        require_cross_dex = False
        prefer_cross_dex = False

    if trigger_max_candidates_raw > 0 and len(candidates) > int(trigger_max_candidates_raw):
        candidates = candidates[: int(trigger_max_candidates_raw)]
        capped_by_trigger = True
    candidates_after_caps = len(candidates)
    if candidates_after_caps == 0:
        return _finalize({
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_routes",
            "candidates_raw": int(candidates_raw),
            "candidates_after_prepare": int(candidates_after_prepare),
            "candidates_after_universe": int(candidates_after_universe or 0),
            "candidates_after_base_filter": int(candidates_after_base_filter),
            "candidates_after_hops_filter": int(candidates_after_hops_filter),
            "candidates_after_cross_dex_filter": 0,
            "candidates_after_viability_filter": None,
            "candidates_after_caps": 0,
            "candidates_scheduled": 0,
            "zero_candidates_reason": "filtered_by_caps",
            "zero_candidates_detail": "max_candidates_cap",
            "zero_candidates_stage": "caps",
            "zero_schedule_reason": "filtered_by_caps",
            "zero_schedule_detail": "max_candidates_cap",
            "base_used": base_used,
            "base_fallback_used": base_fallback_used,
            "hops_attempted": hops_attempted,
            "hop_fallback_used": hop_fallback_used,
            "connectors_added": connectors_added,
            "cross_dex_fallback_used": False,
            "prepare_truncated": bool(prepare_truncated),
            "prepare_time_ms": float(prepare_time_ms) if prepare_time_ms is not None else None,
            "schedule_guard_triggered": False,
            "budget_remaining_ms_at_schedule": None,
        })
    remaining_budget_ms_at_schedule = int(max(0.0, (scan_deadline - loop.time()) * 1000.0))
    min_first_task_ms = int(float(getattr(s, "min_first_task_s", 0.08)) * 1000.0)
    if not candidates:
        return _finalize({
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_routes",
            "candidates_raw": int(candidates_raw),
            "candidates_after_prepare": int(candidates_after_prepare),
            "candidates_after_universe": int(candidates_after_universe or 0),
            "candidates_after_base_filter": int(candidates_after_base_filter),
            "candidates_after_hops_filter": int(candidates_after_hops_filter),
            "candidates_after_cross_dex_filter": 0,
            "candidates_after_viability_filter": None,
            "candidates_after_caps": int(candidates_after_caps),
            "candidates_scheduled": 0,
            "zero_candidates_reason": "no_candidates",
            "zero_candidates_detail": "empty_candidates",
            "zero_candidates_stage": "prepare",
            "zero_schedule_reason": "no_candidates",
            "zero_schedule_detail": "empty_candidates",
            "base_used": base_used,
            "base_fallback_used": base_fallback_used,
            "hops_attempted": hops_attempted,
            "hop_fallback_used": hop_fallback_used,
            "connectors_added": connectors_added,
            "cross_dex_fallback_used": False,
            "prepare_truncated": bool(prepare_truncated),
            "prepare_time_ms": float(prepare_time_ms) if prepare_time_ms is not None else None,
            "schedule_guard_triggered": False,
            "budget_remaining_ms_at_schedule": remaining_budget_ms_at_schedule,
        })
    if remaining_budget_ms_at_schedule < min_first_task_ms:
        return _finalize({
            "scheduled": 0,
            "finished": 0,
            "timeouts": 0,
            "best_gross": 0.0,
            "best_net": 0.0,
            "best_route_summary": None,
            "outcome": "no_budget",
            "candidates_raw": int(candidates_raw),
            "candidates_after_prepare": int(candidates_after_prepare),
            "candidates_after_universe": int(candidates_after_universe or 0),
            "candidates_after_base_filter": int(candidates_after_base_filter),
            "candidates_after_hops_filter": int(candidates_after_hops_filter),
            "candidates_after_cross_dex_filter": int(candidates_after_caps),
            "candidates_after_viability_filter": None,
            "candidates_after_caps": int(candidates_after_caps),
            "candidates_scheduled": 0,
            "zero_candidates_reason": "remaining_budget_too_low",
            "zero_candidates_detail": "min_first_task_ms",
            "zero_candidates_stage": "schedule",
            "zero_schedule_reason": "remaining_budget_too_low",
            "zero_schedule_detail": "min_first_task_ms",
            "base_used": base_used,
            "base_fallback_used": base_fallback_used,
            "hops_attempted": hops_attempted,
            "hop_fallback_used": hop_fallback_used,
            "connectors_added": connectors_added,
            "cross_dex_fallback_used": False,
            "prepare_truncated": bool(prepare_truncated),
            "prepare_time_ms": float(prepare_time_ms) if prepare_time_ms is not None else None,
            "schedule_guard_triggered": True,
            "budget_remaining_ms_at_schedule": remaining_budget_ms_at_schedule,
        })

    payloads, _, scan_stats = await _scan_candidates(
        candidates,
        block_ctx,
        fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
        timeout_s=timeout_s,
        deadline_s=scan_deadline,
        keep_all=True,
    )
    payloads = [p for p in payloads if p and p.get("profit_raw", -1) != -1]
    payloads = [_apply_safety(p, s) for p in payloads]

    scheduled = int(scan_stats.get("scheduled", 0))
    invalid = int(scan_stats.get("invalid", 0))
    candidates_after_viability_filter: Optional[int] = None
    if scheduled > 0:
        candidates_after_viability_filter = max(0, scheduled - invalid)

    def _payload_dexes(p: Dict[str, Any]) -> List[str]:
        dex_path = p.get("dex_path") or p.get("route_dex") or []
        out: List[str] = []
        for d in dex_path:
            if not d:
                continue
            dex = str(d).split(":")[0]
            if dex:
                out.append(dex)
        return out

    def _payload_dex_mix(p: Dict[str, Any]) -> Optional[Dict[str, int]]:
        mix = p.get("dex_mix")
        if isinstance(mix, dict):
            return {str(k): int(v) for k, v in mix.items() if v}
        dexes = _payload_dexes(p)
        if not dexes:
            return None
        out: Dict[str, int] = {}
        for d in dexes:
            out[str(d)] = int(out.get(str(d), 0)) + 1
        return out or None

    def _distinct_dex_count(p: Dict[str, Any]) -> int:
        dexes = _payload_dexes(p)
        return len(set(dexes)) if dexes else 0

    def _top_viability_reason(rejects: Dict[str, int]) -> Optional[str]:
        if not rejects:
            return None
        try:
            return sorted(rejects.items(), key=lambda kv: int(kv[1]), reverse=True)[0][0]
        except Exception:
            return None

    def _map_viability_rejects(rejects: Dict[str, int]) -> Dict[str, int]:
        out = {
            "viability_no_pool": 0,
            "viability_unknown_token": 0,
            "viability_quote_timeout": 0,
            "viability_quote_revert": 0,
            "viability_rpc_error": 0,
            "viability_out_of_sync": 0,
            "viability_price_impact_too_high": 0,
            "viability_gas_too_high": 0,
            "viability_nonsensical_quote": 0,
        }
        for reason, count in (rejects or {}).items():
            if count is None:
                continue
            r = str(reason or "").lower()
            n = int(count)
            if not n:
                continue
            if "nonsensical" in r or "overflow_like" in r:
                out["viability_nonsensical_quote"] += n
            elif "timeout" in r:
                out["viability_quote_timeout"] += n
            elif "revert" in r:
                out["viability_quote_revert"] += n
            elif "out_of_sync" in r:
                out["viability_out_of_sync"] += n
            elif "rpc_error" in r or "http_" in r:
                out["viability_rpc_error"] += n
            elif "impact" in r:
                out["viability_price_impact_too_high"] += n
            elif "gas" in r:
                out["viability_gas_too_high"] += n
            elif "unknown_token" in r:
                out["viability_unknown_token"] += n
            elif "quote_fail" in r or "quote_error" in r or "unknown_pool" in r:
                out["viability_no_pool"] += n
            else:
                out["viability_rpc_error"] += n
        return {k: v for k, v in out.items() if v}

    viability_rejects_by_reason = _map_viability_rejects(scan_stats.get("rejects_by_reason", {}))
    if viability_rejects_by_reason:
        for reason, count in viability_rejects_by_reason.items():
            try:
                METRICS.inc_reason("viability_drop_reason_counts", str(reason), int(count))
            except Exception:
                continue
    candidates_after_cross_dex_filter: Optional[int] = None
    cross_dex_fallback_used = False
    payloads_before_cross = list(payloads)
    if require_cross_dex:
        filtered_payloads = [p for p in payloads if _distinct_dex_count(p) >= 2]
        candidates_after_cross_dex_filter = len(filtered_payloads)
        if payloads and not filtered_payloads:
            if cross_dex_fallback:
                cross_dex_fallback_used = True
                payloads = payloads_before_cross
            else:
                payloads = []
        else:
            payloads = filtered_payloads
    else:
        candidates_after_cross_dex_filter = len(payloads)

    zero_candidates_reason = None
    zero_candidates_detail = None
    zero_candidates_stage = None
    zero_schedule_reason = None
    zero_schedule_detail = None
    schedule_guard_triggered = bool(scan_stats.get("schedule_guard_blocked"))
    top_viability_reason = _top_viability_reason(viability_rejects_by_reason)
    if scheduled == 0:
        reason_if_zero = scan_stats.get("reason_if_zero_scheduled")
        if reason_if_zero == "no_candidates":
            zero_schedule_reason = "no_candidates"
        elif reason_if_zero == "schedule_guard_triggered":
            zero_schedule_reason = "schedule_guard_blocked"
        elif reason_if_zero == "stage1_deadline_exhausted_pre_scan":
            zero_schedule_reason = "deadline_exhausted"
        elif reason_if_zero:
            zero_schedule_reason = "schedule_guard_blocked" if "guard" in str(reason_if_zero) else "deadline_exhausted"
        else:
            zero_schedule_reason = "unknown"
        zero_schedule_detail = str(reason_if_zero) if reason_if_zero else None
        zero_candidates_reason = zero_schedule_reason
        zero_candidates_detail = zero_schedule_detail
        zero_candidates_stage = "schedule"
    elif candidates_after_viability_filter == 0:
        if top_viability_reason:
            zero_candidates_reason = top_viability_reason
            zero_candidates_detail = "viability_rejects"
            zero_candidates_stage = "viability"
        else:
            zero_candidates_reason = "filtered_by_viability"
            zero_candidates_detail = "no_valid_payloads"
            zero_candidates_stage = "viability"
    elif require_cross_dex and not payloads and not cross_dex_fallback:
        zero_candidates_reason = "filtered_by_cross_dex"
        zero_candidates_detail = "require_cross_dex"
        zero_candidates_stage = "cross_dex"

    best_gross = 0.0
    best_net = 0.0
    best_route = None
    best_dex_mix = None
    best_hops = None
    best_reason = None
    best_classification = "no_hit"
    best_backend = "quote"
    sim_ok: Optional[bool] = None
    sim_revert_reason: Optional[str] = None
    arb_calldata_len = None
    arb_calldata_prefix = None
    arb_call = None
    best_candidate_id: Optional[str] = None
    best_route_tokens: Optional[List[str]] = None
    best_hops_payload: Optional[List[Dict[str, Any]]] = None
    best_hop_amounts: Optional[List[Dict[str, Any]]] = None
    best_amount_in: Optional[int] = None
    dryrun_status: Optional[str] = None
    if payloads:
        dec = _decimals_by_token(base_addr)
        def _score_payload(p: Dict[str, Any]) -> float:
            gross = int(p.get("profit_gross_raw", 0) or 0)
            score = float(gross)
            distinct = _distinct_dex_count(p)
            if prefer_cross_dex and distinct >= 2 and cross_bonus:
                score *= 1.0 + (float(cross_bonus) / 10_000.0)
            elif distinct <= 1 and same_penalty:
                score *= max(0.0, 1.0 - (float(same_penalty) / 10_000.0))
            return score
        ranked = sorted(payloads, key=_score_payload, reverse=True)
        best = ranked[0]
        best_candidate_id = best.get("candidate_id")
        best_route = _route_pretty(tuple(best.get("route")), best.get("route_dex"), best.get("route_fee_bps"), best.get("route_fee_tier"), best.get("dex_path"))
        best_route_tokens = list(best.get("route") or [])
        best_hops_payload = list(best.get("hops") or []) if isinstance(best.get("hops"), list) else None
        best_hop_amounts = list(best.get("hop_amounts") or []) if isinstance(best.get("hop_amounts"), list) else None
        try:
            best_amount_in = int(best.get("amount_in", 0) or 0)
        except Exception:
            best_amount_in = None
        if getattr(config, "ARB_EXECUTOR_ADDRESS", ""):
            try:
                sim_to_addr = getattr(config, "ARB_EXECUTOR_OWNER", "") or getattr(config, "SIM_FROM_ADDRESS", SIM_FROM_ADDRESS)
                arb_call = preflight.build_arb_calldata(best, s, to_addr=sim_to_addr)
                arb_calldata_len = int(len(arb_call))
                arb_calldata_prefix = "0x" + arb_call.hex()[:16]
            except Exception:
                arb_call = None
        sim_backend = str(getattr(s, "sim_backend", "") or "").strip().lower()
        backend_list = [sim_backend] if sim_backend else list(getattr(config, "EXEC_SIM_BACKENDS", ["quote"]))
        sim = await simulate_candidate_async(
            best,
            s,
            backends=backend_list,
            rpc=PS.rpc,
            block_ctx=block_ctx,
            arb_call=arb_call,
        )
        gross_raw = int(sim.gross_raw)
        best_gross = float(gross_raw) / float(10 ** dec)
        best_net = float(int(sim.net_after_buffers_raw)) / float(10 ** dec)
        best_classification = sim.classification
        best_backend = sim.backend
        sim_ok = sim.sim_ok
        sim_revert_reason = sim.sim_revert_reason
        best_dex_mix = _payload_dex_mix(best)
        best_hops = best.get("hops_count") or (len(best.get("route", ())) - 1 if best.get("route") else None)
        best_reason = best.get("reason_selected")
        if best_reason is None:
            distinct = _distinct_dex_count(best)
            if prefer_cross_dex and distinct >= 2 and cross_bonus:
                best_reason = "cross_dex_bonus"
            elif distinct <= 1 and same_penalty:
                best_reason = "same_dex_penalty"
        if str(getattr(s, "execution_mode", "off")).lower() == "dryrun":
            try:
                dryrun_entry = await build_tx_ready(best, s, rpc=PS.rpc, block_ctx=block_ctx)
                dryrun_status = dryrun_entry.get("status") if isinstance(dryrun_entry, dict) else None
            except Exception:
                dryrun_status = "error"

    outcome = "no_hit"
    if best_classification in ("net_hit", "valid_hit"):
        outcome = "net_hit"
    elif best_classification == "gross_hit":
        outcome = "gross_hit"

    return _finalize({
        "scheduled": int(scan_stats.get("scheduled", 0)),
        "finished": int(scan_stats.get("finished", 0)),
        "timeouts": int(scan_stats.get("timeouts", 0)),
        "best_gross": float(best_gross),
        "best_net": float(best_net),
        "best_route_summary": best_route,
        "best_route_tokens": best_route_tokens,
        "best_hops": best_hops_payload,
        "best_hop_amounts": best_hop_amounts,
        "best_amount_in": best_amount_in,
        "candidate_id": best_candidate_id,
        "dex_mix": best_dex_mix,
        "hops": best_hops,
        "reason_selected": best_reason,
        "classification": best_classification,
        "backend": best_backend,
        "sim_ok": sim_ok,
        "sim_revert_reason": sim_revert_reason,
        "arb_calldata_len": arb_calldata_len,
        "arb_calldata_prefix": arb_calldata_prefix,
        "dryrun_status": dryrun_status,
        "outcome": outcome,
        "candidates_raw": int(candidates_raw),
        "candidates_after_prepare": int(candidates_after_prepare),
        "candidates_after_universe": int(candidates_after_universe or 0),
        "candidates_after_base_filter": int(candidates_after_base_filter),
        "candidates_after_hops_filter": int(candidates_after_hops_filter),
        "candidates_after_cross_dex_filter": candidates_after_cross_dex_filter,
        "candidates_after_viability_filter": candidates_after_viability_filter,
        "candidates_after_caps": int(candidates_after_caps),
        "candidates_scheduled": int(scan_stats.get("scheduled", 0)),
        "zero_candidates_reason": zero_candidates_reason,
        "zero_candidates_detail": zero_candidates_detail,
        "zero_candidates_stage": zero_candidates_stage,
        "top_viability_drop_reason": top_viability_reason,
        "zero_schedule_reason": zero_schedule_reason,
        "zero_schedule_detail": zero_schedule_detail,
        "base_used": base_used,
        "base_fallback_used": bool(base_fallback_used),
        "hops_attempted": list(hops_attempted),
        "hop_fallback_used": bool(hop_fallback_used),
        "connectors_added": connectors_added,
        "schedule_guard_remaining_ms": scan_stats.get("schedule_guard_remaining_ms"),
        "schedule_guard_blocked": scan_stats.get("schedule_guard_blocked"),
        "schedule_guard_triggered": bool(schedule_guard_triggered),
        "schedule_guard_reason": scan_stats.get("schedule_guard_reason"),
        "budget_remaining_ms_at_schedule": scan_stats.get("budget_remaining_ms_at_schedule"),
        "viability_rejects_by_reason": viability_rejects_by_reason,
        "capped_by_trigger_max_candidates_raw": bool(capped_by_trigger),
        "cross_dex_fallback_used": bool(cross_dex_fallback_used),
        "prepare_truncated": bool(prepare_truncated),
        "prepare_time_ms": float(prepare_time_ms) if prepare_time_ms is not None else None,
    })


def _make_block_context(block_number: int, *, pinned_http_endpoint: Optional[str] = None) -> BlockContext:
    return BlockContext(
        block_number=int(block_number),
        block_tag=hex(int(block_number)),
        pinned_http_endpoint=str(pinned_http_endpoint).strip() if pinned_http_endpoint else None,
    )


_last_rpc_latency_ms: Optional[float] = None


def _rpc_latency_ms() -> Optional[float]:
    global _last_rpc_latency_ms
    try:
        if hasattr(PS.rpc, "stats"):
            stats = PS.rpc.stats()
            vals = [float(s.get("lat_ms", 0.0)) for s in stats if isinstance(s, dict) and s.get("lat_ms") is not None]
            vals = [v for v in vals if v > 0]
            if vals:
                vals.sort()
                mid = vals[len(vals) // 2]
                _last_rpc_latency_ms = float(mid)
                return _last_rpc_latency_ms
    except Exception:
        pass
    return _last_rpc_latency_ms


def _prepare_budget_s(s: Settings) -> float:
    ratio = float(getattr(s, "prepare_budget_ratio", 0.20))
    base = float(getattr(s, "block_budget_s", 10.0))
    min_s = float(getattr(s, "prepare_budget_min_s", 2.0))
    max_s = float(getattr(s, "prepare_budget_max_s", 6.0))
    budget = base * ratio
    if max_s < min_s:
        max_s = min_s
    return float(max(min_s, min(max_s, budget)))


def _compute_scan_budget_s(block_budget_s: float, elapsed_s: float, *, min_scan_s: float = 1.0) -> float:
    remaining = float(block_budget_s) - float(elapsed_s)
    if remaining < float(min_scan_s):
        return float(min_scan_s)
    return float(remaining)


def _adaptive_stage1_ratio(candidate_estimate: int, rpc_latency_ms: Optional[float]) -> float:
    # Base ratio favors stage1 filtering, but adapts when RPC is slow or candidates are many.
    ratio = 0.55
    if candidate_estimate >= 150:
        ratio += 0.05
    if candidate_estimate >= 250:
        ratio += 0.05
    if candidate_estimate >= 350:
        ratio += 0.05
    if rpc_latency_ms is not None:
        if rpc_latency_ms > 900:
            ratio += 0.05
        if rpc_latency_ms > 1300:
            ratio += 0.05
    return float(max(0.45, min(0.75, ratio)))


def _expand_budget_s(stage1_window_s: float, s: Settings) -> float:
    cap = float(getattr(s, "expand_ratio_cap", 0.60))
    max_s = float(getattr(s, "expand_budget_max_s", 2.0))
    if max_s <= 0:
        max_s = 0.5
    if bool(getattr(s, "enable_multidex", False)) and max_s < 6.0:
        max_s = 6.0
    target = stage1_window_s * cap
    if bool(getattr(s, "enable_multidex", False)) and target < 4.5:
        target = 4.5
    return float(min(target, max_s))


def _cap_int(value: int, *, min_value: int = 1) -> int:
    try:
        v = int(value)
    except Exception:
        v = min_value
    if v < min_value:
        return int(min_value)
    return int(v)


def _should_fallback_block(stats: Dict[str, int], *, candidates_count: int) -> bool:
    min_candidates = int(getattr(config, "RPC_OUT_OF_SYNC_MIN_CANDIDATES", 40))
    if candidates_count < min_candidates:
        return False
    scheduled = int(stats.get("scheduled", 0))
    if scheduled <= 0:
        return False
    failures = int(stats.get("invalid", 0)) + int(stats.get("timeouts", 0))
    ratio = float(failures) / float(max(1, scheduled))
    threshold = float(getattr(config, "RPC_OUT_OF_SYNC_FAIL_RATIO", 0.6))
    return ratio >= threshold


async def _gas_gwei(timeout_s: float = 2.0) -> Optional[float]:
    try:
        gp = await PS.rpc.call("eth_gasPrice", [], timeout_s=timeout_s)
        return float(int(gp, 16)) / 1e9
    except Exception:
        return None


async def _route_viable(
    route: Tuple[str, ...],
    *,
    block_ctx: BlockContext,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    probe_units: float,
    viability_cache: Optional[Dict[Tuple[str, str, str, int], Optional[int]]] = None,
) -> bool:
    amt = _scale_amount(route[0], probe_units)
    for i in range(len(route) - 1):
        key = (
            str(block_ctx.block_tag),
            str(route[i]).lower(),
            str(route[i + 1]).lower(),
            int(amt),
        )
        if viability_cache is not None and getattr(config, "VIABILITY_CACHE_ENABLED", True):
            if key in viability_cache:
                cached_out = viability_cache[key]
                if not cached_out or int(cached_out) <= 0:
                    return False
                amt = int(cached_out)
                continue
        edge = await PS.best_edge(
            route[i],
            route[i + 1],
            amt,
            block_ctx=block_ctx,
            fee_tiers=fee_tiers,
            timeout_s=timeout_s,
        )
        if not edge or int(edge.amount_out) <= 0:
            if viability_cache is not None and getattr(config, "VIABILITY_CACHE_ENABLED", True):
                viability_cache[key] = None
            return False
        if viability_cache is not None and getattr(config, "VIABILITY_CACHE_ENABLED", True):
            viability_cache[key] = int(edge.amount_out)
        amt = int(edge.amount_out)
    return True


def _edge_to_hop(edge: Any, token_in: str, token_out: str) -> Hop:
    params: Dict[str, Any] = {}
    try:
        tier = edge.meta.get("fee_tier") if getattr(edge, "meta", None) else None
        if tier is not None:
            params["fee_tier"] = int(tier)
    except Exception:
        pass
    return Hop(token_in=token_in, token_out=token_out, dex_id=str(edge.dex_id), params=params)


async def _beam_candidates_for_route(
    route: Tuple[str, ...],
    *,
    amount_in: int,
    block_ctx: BlockContext,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    beam_k: int,
    edge_top_m: int,
    edge_top_m_per_dex: Optional[int] = None,
    eval_budget: int,
    prefer_cross_dex: bool = False,
    require_cross_dex: bool = False,
    cross_dex_bonus_bps: float = 0.0,
    same_dex_penalty_bps: float = 0.0,
    deadline_s: Optional[float] = None,
    stats: Optional[Dict[str, Any]] = None,
    enforce_cross_dex: bool = True,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    states: List[Dict[str, Any]] = [{"amount": int(amount_in), "hops": [], "dexes": [], "score": float(amount_in)}]
    max_drawdown = float(getattr(config, "BEAM_MAX_DRAWDOWN", 0.35))
    max_drawdown = min(0.9, max(0.0, max_drawdown))

    for i in range(len(route) - 1):
        if deadline_s is not None and loop.time() >= deadline_s:
            return []
        token_in = route[i]
        token_out = route[i + 1]
        new_states: List[Dict[str, Any]] = []
        for st in states:
            if deadline_s is not None and loop.time() >= deadline_s:
                break
            edges = await PS.quote_edges(
                token_in,
                token_out,
                int(st["amount"]),
                block_ctx=block_ctx,
                fee_tiers=fee_tiers,
                timeout_s=timeout_s,
            )
            if not edges:
                continue
            if edge_top_m_per_dex:
                edges_by_dex: Dict[str, List[Any]] = {}
                for edge in edges:
                    edges_by_dex.setdefault(str(edge.dex_id), []).append(edge)
                filtered: List[Any] = []
                for dex_id, group in edges_by_dex.items():
                    group = sorted(group, key=lambda e: int(e.amount_out), reverse=True)
                    filtered.extend(group[: max(1, int(edge_top_m_per_dex))])
                edges = filtered
            else:
                edges = sorted(edges, key=lambda e: int(e.amount_out), reverse=True)
                edges = edges[: max(1, int(edge_top_m))]
            for edge in edges:
                if len(new_states) >= int(eval_budget):
                    break
                if int(edge.amount_out) <= 0:
                    continue
                next_hops = list(st["hops"]) + [_edge_to_hop(edge, token_in, token_out)]
                next_dexes = list(st.get("dexes") or []) + [str(edge.dex_id)]
                distinct = len(set(next_dexes))
                score = float(edge.amount_out)
                reason = st.get("reason_selected")
                if distinct >= 2 and (prefer_cross_dex or require_cross_dex):
                    if cross_dex_bonus_bps:
                        score *= 1.0 + (float(cross_dex_bonus_bps) / 10_000.0)
                    reason = "cross_dex_bonus"
                elif distinct == 1 and same_dex_penalty_bps:
                    score *= max(0.0, 1.0 - (float(same_dex_penalty_bps) / 10_000.0))
                    reason = "same_dex_penalty"
                new_states.append(
                    {
                        "amount": int(edge.amount_out),
                        "hops": next_hops,
                        "dexes": next_dexes,
                        "score": score,
                        "reason_selected": reason,
                    }
                )
            if len(new_states) >= int(eval_budget):
                break

        if not new_states:
            return []

        new_states = sorted(new_states, key=lambda st: float(st.get("score", st.get("amount", 0))), reverse=True)
        if max_drawdown > 0:
            floor_amt = int(float(amount_in) * (1.0 - max_drawdown))
            new_states = [st for st in new_states if int(st["amount"]) >= floor_amt]
        states = new_states[: max(1, int(beam_k))]

        if not states:
            return []

    if require_cross_dex and enforce_cross_dex:
        states = [st for st in states if len(set(st.get("dexes") or [])) >= 2]
        if not states:
            return []

    out: List[Dict[str, Any]] = []
    for st in states:
        dex_mix: Dict[str, int] = {}
        for hop in st.get("hops", []):
            dex_mix[str(hop.dex_id)] = int(dex_mix.get(str(hop.dex_id), 0)) + 1
        out.append({"route": route, "hops": list(st["hops"]), "amount_in": int(amount_in)})
        out[-1]["dex_mix"] = dex_mix
        out[-1]["hops_count"] = int(len(route) - 1)
        out[-1]["reason_selected"] = st.get("reason_selected")
    if stats is not None:
        stats["beam_ms"] = float(stats.get("beam_ms", 0.0)) + (loop.time() - t0) * 1000.0
        stats["beam_candidates_total"] = int(stats.get("beam_candidates_total", 0)) + len(out)
    return out


async def _expand_multidex_candidates(
    routes: List[Tuple[str, ...]],
    *,
    amount_in: int,
    block_ctx: BlockContext,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    deadline_s: float,
    settings: Settings,
    max_total: Optional[int] = None,
    max_per_route: Optional[int] = None,
    viability_cache: Optional[Dict[Tuple[str, str, str, int], Optional[int]]] = None,
    stats: Optional[Dict[str, Any]] = None,
    edge_top_m_per_dex: Optional[int] = None,
    prefer_cross_dex: bool = False,
    require_cross_dex: bool = False,
    cross_dex_bonus_bps: float = 0.0,
    same_dex_penalty_bps: float = 0.0,
    skip_viability: bool = False,
    enforce_cross_dex: bool = True,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    expand_t0 = loop.time()
    sem = asyncio.Semaphore(max(1, int(settings.concurrency)))
    results: List[Dict[str, Any]] = []
    if stats is not None:
        stats.setdefault("beam_ms", 0.0)
        stats.setdefault("beam_candidates_total", 0)
        stats.setdefault("capped_by_max_total_expanded", False)
        stats.setdefault("capped_by_max_expanded_per_candidate", False)
        stats.setdefault("routes_total", 0)
        stats.setdefault("routes_with_candidates", 0)
        stats.setdefault("routes_without_candidates", 0)
        stats.setdefault("routes_errors", 0)

    async def one(route: Tuple[str, ...]) -> List[Dict[str, Any]]:
        async with sem:
            if loop.time() >= deadline_s:
                return []
            try:
                if not skip_viability:
                    ok = await _route_viable(
                        route,
                        block_ctx=block_ctx,
                        fee_tiers=fee_tiers,
                        timeout_s=timeout_s,
                        probe_units=float(settings.probe_amount),
                        viability_cache=viability_cache,
                    )
                    if not ok:
                        if stats is not None:
                            stats["routes_without_candidates"] = int(stats.get("routes_without_candidates", 0)) + 1
                        return []
                time_left = float(deadline_s) - float(loop.time())
                beam_k = int(settings.beam_k)
                edge_top_m_eff = int(settings.edge_top_m)
                if time_left < 6.0:
                    beam_k = min(beam_k, 8)
                    edge_top_m_eff = min(edge_top_m_eff, 2)
                out = await _beam_candidates_for_route(
                    route,
                    amount_in=int(amount_in),
                    block_ctx=block_ctx,
                    fee_tiers=fee_tiers,
                    timeout_s=timeout_s,
                    beam_k=int(beam_k),
                    edge_top_m=int(edge_top_m_eff),
                    edge_top_m_per_dex=edge_top_m_per_dex,
                    eval_budget=int(settings.stage2_max_evals),
                    prefer_cross_dex=prefer_cross_dex,
                    require_cross_dex=require_cross_dex,
                    cross_dex_bonus_bps=cross_dex_bonus_bps,
                    same_dex_penalty_bps=same_dex_penalty_bps,
                    deadline_s=deadline_s,
                    stats=stats,
                    enforce_cross_dex=enforce_cross_dex,
                )
                if stats is not None:
                    if out:
                        stats["routes_with_candidates"] = int(stats.get("routes_with_candidates", 0)) + 1
                    else:
                        stats["routes_without_candidates"] = int(stats.get("routes_without_candidates", 0)) + 1
                if max_per_route is not None and max_per_route > 0 and len(out) > int(max_per_route):
                    if stats is not None:
                        stats["capped_by_max_expanded_per_candidate"] = True
                if max_per_route is not None and max_per_route > 0:
                    out = out[: int(max_per_route)]
                return out
            except Exception:
                if stats is not None:
                    stats["routes_errors"] = int(stats.get("routes_errors", 0)) + 1
                return []

    tasks: List[asyncio.Task] = []
    for route in routes:
        if loop.time() >= deadline_s:
            break
        if max_total is not None and max_total > 0 and len(results) >= int(max_total):
            if stats is not None:
                stats["capped_by_max_total_expanded"] = True
            break
        if stats is not None:
            stats["routes_total"] = int(stats.get("routes_total", 0)) + 1
        tasks.append(asyncio.create_task(one(route)))

    try:
        pending = set(tasks)
        while pending and loop.time() < deadline_s:
            timeout = max(0.0, float(deadline_s) - float(loop.time()))
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                res = await d
                if res:
                    results.extend(res)
                if max_total is not None and max_total > 0 and len(results) >= int(max_total):
                    if stats is not None:
                        stats["capped_by_max_total_expanded"] = True
                    pending = set()
                    break
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if max_total is not None and max_total > 0:
        results = results[: int(max_total)]
    if stats is not None:
        stats["expand_ms"] = float(stats.get("expand_ms", 0.0)) + (loop.time() - expand_t0) * 1000.0
        stats["candidates_after_multidex"] = int(len(results))
        stats["candidates_after_beam"] = int(stats.get("beam_candidates_total", len(results)))
    return results
    return results


async def _scan_candidates(
    candidates: List[Dict[str, Any]],
    block_ctx: BlockContext,
    *,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    deadline_s: float,
    keep_all: bool,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    """Scan candidates until deadline.

    Returns (payloads, finished_count)
    """

    s = _load_settings()
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(max(1, int(s.concurrency)))
    stats_lock = asyncio.Lock()
    results: List[Dict[str, Any]] = []
    finished = 0
    invalid = 0
    sanity_rejects_total = 0
    rejects_by_reason: Dict[str, int] = {}
    reason_if_zero_scheduled: Optional[str] = None
    schedule_guard_s = min(0.10, max(0.02, float(timeout_s) * 0.10))
    min_first_task_s = float(getattr(s, "min_first_task_s", 0.08))
    schedule_at_least_one = False
    schedule_start_t = loop.time()
    remaining_before_scheduling_ms = int(max(0.0, (deadline_s - schedule_start_t) * 1000.0))
    schedule_guard_blocked = False
    schedule_guard_reason: Optional[str] = None
    schedule_guard_remaining_ms: Optional[int] = None

    async def one(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal finished
        nonlocal invalid
        nonlocal sanity_rejects_total
        nonlocal rejects_by_reason
        async with sem:
            try:
                # Enforce per-candidate timeout hard
                payload = await asyncio.wait_for(
                    PS.price_payload(c, block_ctx=block_ctx, fee_tiers=fee_tiers, timeout_s=timeout_s),
                    timeout=timeout_s + 0.5,
                )
            except Exception:
                payload = None
            if payload and isinstance(c, dict):
                if c.get("reason_selected") is not None:
                    payload["reason_selected"] = c.get("reason_selected")
            async with stats_lock:
                if not payload or payload.get("profit_raw", -1) == -1:
                    invalid += 1
                if payload and isinstance(payload.get("error"), dict):
                    err = payload.get("error") or {}
                    reason = str(err.get("reason") or err.get("kind") or "unknown")
                    rejects_by_reason[reason] = int(rejects_by_reason.get(reason, 0)) + 1
                    if str(err.get("kind")).lower() == "sanity":
                        sanity_rejects_total += 1
                finished += 1
            return payload

    tasks: List[asyncio.Task] = []
    if not candidates:
        reason_if_zero_scheduled = "no_candidates"
    def _candidate_score(c: Dict[str, Any]) -> int:
        try:
            return int(c.get("amount_in", 0))
        except Exception:
            return 0

    best_candidate = None
    if candidates:
        try:
            best_candidate = max(candidates, key=_candidate_score)
        except Exception:
            best_candidate = candidates[0]

    for c in candidates:
        now = loop.time()
        remaining = deadline_s - now
        if remaining <= 0:
            if not tasks:
                reason_if_zero_scheduled = "stage1_deadline_exhausted_pre_scan"
            break
        if remaining < schedule_guard_s:
            schedule_guard_blocked = True
            schedule_guard_remaining_ms = int(max(0.0, remaining * 1000.0))
            if not tasks and remaining >= min_first_task_s and best_candidate is not None:
                tasks.append(asyncio.create_task(one(best_candidate)))
                schedule_at_least_one = True
                schedule_guard_reason = "schedule_at_least_one"
            if not tasks and not reason_if_zero_scheduled:
                reason_if_zero_scheduled = "schedule_guard_triggered"
                schedule_guard_reason = "schedule_guard_triggered"
            break
        tasks.append(asyncio.create_task(one(c)))
    scheduled = len(tasks)
    budget_skipped = max(0, len(candidates) - scheduled)
    if scheduled == 0 and not reason_if_zero_scheduled:
        if loop.time() >= deadline_s:
            reason_if_zero_scheduled = "stage1_deadline_exhausted_pre_scan"
        else:
            reason_if_zero_scheduled = "schedule_unknown_zero"

    schedule_ms = (loop.time() - schedule_start_t) * 1000.0

    try:
        # Avoid asyncio.as_completed(...)+break warnings ("_wait_for_one was never awaited").
        pending = set(tasks)
        await_start_t = loop.time()
        while pending and loop.time() < deadline_s:
            timeout = max(0.0, float(deadline_s) - float(loop.time()))
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                p = await d
                if not p:
                    continue
                if keep_all:
                    results.append(p)
                else:
                    # strict profitable only (actual thresholds applied later)
                    if p.get("profit_raw", -1) > 0:
                        results.append(p)
        await_ms = (loop.time() - await_start_t) * 1000.0
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    timeouts = max(0, scheduled - finished)
    stats = {
        "scheduled": int(scheduled),
        "finished": int(finished),
        "timeouts": int(timeouts),
        "invalid": int(invalid),
        "budget_skipped": int(budget_skipped),
        "sanity_rejects_total": int(sanity_rejects_total),
        "rejects_by_reason": dict(rejects_by_reason),
        "scan_schedule_ms": float(max(0.0, schedule_ms)),
        "scan_await_ms": float(max(0.0, await_ms if "await_ms" in locals() else 0.0)),
        "stage1_remaining_ms_before_scheduling": int(remaining_before_scheduling_ms),
        "budget_remaining_ms_at_schedule": int(remaining_before_scheduling_ms),
        "schedule_at_least_one": bool(schedule_at_least_one),
        "schedule_guard_remaining_ms": schedule_guard_remaining_ms,
        "schedule_guard_blocked": bool(schedule_guard_blocked),
        "schedule_guard_reason": schedule_guard_reason,
    }
    if reason_if_zero_scheduled:
        stats["reason_if_zero_scheduled"] = str(reason_if_zero_scheduled)
    return results, finished, stats


def _apply_safety(p: Dict[str, Any], s: Settings) -> Dict[str, Any]:
    """Apply slippage safety to profit values (conservative)."""
    try:
        route_t = tuple(p.get("route") or ())
        token_in = route_t[0] if route_t else p.get("token_in")
        if not token_in:
            return p
        amt_in = int(p.get("amount_in", 0))
        dec = _decimals_by_token(str(token_in))
        slip_raw = int(amt_in * float(s.slippage_bps) / 10_000.0)
        mev_raw = int(amt_in * float(s.mev_buffer_bps) / 10_000.0)
        safety_raw = int(slip_raw + mev_raw)
        gas_cost = int(p.get("gas_cost", 0) or 0)

        profit_gross_raw = p.get("profit_gross_raw", p.get("profit_raw_no_gas", None))
        if profit_gross_raw is None:
            try:
                profit_gross_raw = int(p.get("amount_out", 0)) - int(amt_in)
            except Exception:
                profit_gross_raw = int(p.get("profit_raw", 0) or 0) + int(gas_cost)

        profit_net_raw = int(profit_gross_raw) - int(gas_cost)
        profit_safety_raw = int(profit_net_raw) - int(safety_raw)

        amt_in_f = float(amt_in) / float(10 ** dec) if dec > 0 else 0.0
        profit_gross = float(profit_gross_raw) / float(10 ** dec)
        profit_net = float(profit_net_raw) / float(10 ** dec)
        profit_safety = float(profit_safety_raw) / float(10 ** dec)
        profit_pct_gross = (profit_gross / amt_in_f * 100.0) if amt_in_f > 0 else 0.0
        profit_pct_net = (profit_net / amt_in_f * 100.0) if amt_in_f > 0 else 0.0
        profit_pct_safety = (profit_safety / amt_in_f * 100.0) if amt_in_f > 0 else 0.0

        p2 = dict(p)
        p2["slippage_raw"] = int(slip_raw)
        p2["mev_buffer_raw"] = int(mev_raw)
        p2["profit_gross_raw"] = int(profit_gross_raw)
        p2["profit_gross"] = float(profit_gross)
        p2["profit_pct_gross"] = float(profit_pct_gross)
        p2["profit_raw_net"] = int(profit_net_raw)
        p2["profit_net"] = float(profit_net)
        p2["profit_pct_net"] = float(profit_pct_net)
        p2["profit_raw_safety"] = int(profit_safety_raw)
        p2["profit_safety"] = float(profit_safety)
        p2["profit_pct_safety"] = float(profit_pct_safety)
        # Backwards-compatible fields used by existing thresholds/UI
        p2["profit_raw_no_gas"] = int(profit_gross_raw)
        p2["profit_no_gas"] = float(profit_gross)
        p2["profit_pct_no_gas"] = float(profit_pct_gross)
        p2["profit_raw_adj"] = int(profit_safety_raw)
        p2["profit_adj"] = float(profit_safety)
        p2["profit_pct_adj"] = float(profit_pct_safety)
        return p2
    except Exception:
        return p


def _passes_thresholds(p: Dict[str, Any], s: Settings) -> bool:
    try:
        p_adj = float(p.get("profit_adj", p.get("profit", 0.0)))
        if p_adj <= 0:
            return False

        # absolute
        if p_adj < float(s.min_profit_abs):
            return False

        # pct (note: s.min_profit_pct is in percent units like 0.05 == 0.05%)
        amt_in = float(p.get("amount_in", 0)) / float(10 ** _decimals_by_token(p.get("route")[0]))
        pct = (p_adj / amt_in * 100.0) if amt_in > 0 else 0.0
        if pct < float(s.min_profit_pct):
            return False

        return True
    except Exception:
        return False


def _funnel_counts(payloads: List[Dict[str, Any]], s: Settings) -> Dict[str, int]:
    counts = {"raw": 0, "gas": 0, "safety": 0, "ready": 0}
    for p in payloads:
        try:
            pr_gross = p.get("profit_gross_raw", p.get("profit_raw_no_gas", None))
            if pr_gross is None:
                pr_gross = int(p.get("amount_out", 0)) - int(p.get("amount_in", 0))
            pr_gross = int(pr_gross)
        except Exception:
            pr_gross = -1
        try:
            pr_net = int(p.get("profit_raw_net", p.get("profit_raw", -1)))
        except Exception:
            pr_net = -1
        try:
            pr_safety = int(p.get("profit_raw_safety", pr_net))
        except Exception:
            pr_safety = -1

        if pr_gross > 0:
            counts["raw"] += 1
        if pr_net > 0:
            counts["gas"] += 1
        if pr_safety > 0:
            counts["safety"] += 1
        if strategies.risk_check(p) and _passes_thresholds(p, s):
            counts["ready"] += 1
    return counts


def _funnel_stats(payloads: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    gross_vals: List[int] = []
    net_vals: List[int] = []
    gas_units_vals: List[int] = []
    gas_cost_vals: List[int] = []
    for p in payloads:
        try:
            gross_vals.append(int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0)))
        except Exception:
            pass
        try:
            net_vals.append(int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0)))
        except Exception:
            pass
        try:
            gas_units_vals.append(int(p.get("gas_units", 0) or 0))
        except Exception:
            pass
        try:
            gas_cost_vals.append(int(p.get("gas_cost", 0) or 0))
        except Exception:
            pass

    def _avg(vals: List[int]) -> Optional[float]:
        if not vals:
            return None
        return float(sum(vals) / max(1, len(vals)))

    def _median(vals: List[int]) -> Optional[float]:
        if not vals:
            return None
        try:
            return float(statistics.median(vals))
        except Exception:
            return None

    def _min(vals: List[int]) -> Optional[int]:
        return min(vals) if vals else None

    def _max(vals: List[int]) -> Optional[int]:
        return max(vals) if vals else None

    return {
        "gross_min": _min(gross_vals),
        "gross_max": _max(gross_vals),
        "net_min": _min(net_vals),
        "net_max": _max(net_vals),
        "gas_units_avg": _avg(gas_units_vals),
        "gas_units_median": _median(gas_units_vals),
        "gas_cost_avg": _avg(gas_cost_vals),
        "gas_cost_median": _median(gas_cost_vals),
        "gas_units_min": _min(gas_units_vals),
        "gas_units_max": _max(gas_units_vals),
        "gas_cost_min": _min(gas_cost_vals),
        "gas_cost_max": _max(gas_cost_vals),
    }


def _top_examples(payloads: List[Dict[str, Any]], *, limit: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    def _entry(p: Dict[str, Any]) -> Dict[str, Any]:
        route_str = _route_pretty(
            tuple(p.get("route") or ()),
            p.get("route_dex"),
            p.get("route_fee_bps"),
            p.get("route_fee_tier"),
            p.get("dex_path"),
        )
        return {
            "route": route_str,
            "dex_path": list(p.get("dex_path") or ()),
            "profit_gross_raw": int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0)),
            "profit_net_raw": int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0)),
            "gas_cost_in": int(p.get("gas_cost", 0) or 0),
            "profit_gross": float(p.get("profit_gross", 0.0)),
            "profit_net": float(p.get("profit_net", p.get("profit", 0.0))),
        }

    gross_sorted = sorted(
        payloads,
        key=lambda p: int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0)),
        reverse=True,
    )
    net_sorted = sorted(
        payloads,
        key=lambda p: int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0)),
        reverse=True,
    )
    return {
        "top_gross": [_entry(p) for p in gross_sorted[:limit]],
        "top_net": [_entry(p) for p in net_sorted[:limit]],
    }


def _reject_reason(p: Dict[str, Any], s: Settings) -> str:
    try:
        gross = int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0))
        net = int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0))
        safety = int(p.get("profit_raw_safety", net))
    except Exception:
        return "bad_payload"

    if gross <= 0:
        return "gross<=0"
    if net <= 0:
        return "net<=0"
    if safety <= 0:
        return "slippage+mev"
    if not strategies.risk_check(p):
        return "risk"
    if not _passes_thresholds(p, s):
        return "thresholds"
    return "ready"


def _debug_funnel_log(
    payloads: List[Dict[str, Any]],
    s: Settings,
    block_number: int,
    stats: Optional[Dict[str, int]] = None,
) -> None:
    if not _env_flag("DEBUG_FUNNEL"):
        return
    if stats:
        print(
            f"[Funnel] Block {block_number} | "
            f"scheduled={stats.get('scheduled', 0)} finished={stats.get('finished', 0)} "
            f"invalid={stats.get('invalid', 0)} timeouts={stats.get('timeouts', 0)} "
            f"budget_skipped={stats.get('budget_skipped', 0)}"
        )
    if not payloads:
        print(f"[Funnel] Block {block_number} | no payloads")
        return

    def _dec_for_payload(p: Dict[str, Any]) -> int:
        try:
            route_t = tuple(p.get("route") or ())
            token_in = route_t[0] if route_t else p.get("token_in")
            return _decimals_by_token(str(token_in))
        except Exception:
            return 18

    def _fmt_units(raw: int, dec: int) -> str:
        try:
            return f"{float(raw) / float(10 ** dec):.6f}"
        except Exception:
            return str(raw)

    gross_sorted = sorted(payloads, key=lambda p: int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0)), reverse=True)
    net_sorted = sorted(payloads, key=lambda p: int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0)), reverse=True)

    stats = _funnel_stats(payloads)
    print(
        f"[Funnel] Block {block_number} | "
        f"gross_min={stats.get('gross_min')} gross_max={stats.get('gross_max')} "
        f"net_min={stats.get('net_min')} net_max={stats.get('net_max')} "
        f"gas_units_avg={stats.get('gas_units_avg')} gas_units_median={stats.get('gas_units_median')} "
        f"gas_cost_avg={stats.get('gas_cost_avg')} gas_cost_median={stats.get('gas_cost_median')}"
    )
    print(f"[Funnel] Block {block_number} | top gross/net candidates:")
    for label, items in (("gross", gross_sorted[:5]), ("net", net_sorted[:5])):
        for idx, p in enumerate(items, start=1):
            dec = _dec_for_payload(p)
            amt_in = int(p.get("amount_in", 0) or 0)
            amt_out = int(p.get("amount_out", 0) or 0)
            gross_raw = int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0))
            net_raw = int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0))
            gas_units = int(p.get("gas_units", 0) or 0)
            gas_cost = int(p.get("gas_cost", 0) or 0)
            route = _route_pretty(
                tuple(p.get("route") or ()),
                p.get("route_dex"),
                p.get("route_fee_bps"),
                p.get("route_fee_tier"),
                p.get("dex_path"),
            )
            fee_tiers = p.get("route_fee_tier")
            reason = _reject_reason(p, s)
            print(
                f"  [{label}#{idx}] route={route} fee_tier={fee_tiers} "
                f"amt_in={_fmt_units(amt_in, dec)} amt_out={_fmt_units(amt_out, dec)} "
                f"gross={_fmt_units(gross_raw, dec)} gas_units={gas_units} "
                f"gas_cost={_fmt_units(gas_cost, dec)} net={_fmt_units(net_raw, dec)} "
                f"reason={reason}"
            )


async def _optimize_amount_for_route(
    route: Tuple[str, ...],
    block_ctx: BlockContext,
    seed_payload: Dict[str, Any],
    *,
    deadline_s: float,
) -> Optional[Dict[str, Any]]:
    """Local optimization (golden-section-ish) with a strict eval budget."""

    s = _load_settings()
    loop = asyncio.get_running_loop()

    token_in = route[0]
    lo_u = float(s.stage2_amount_min)
    hi_u = float(s.stage2_amount_max)
    if hi_u <= lo_u:
        return seed_payload

    lo = _scale_amount(token_in, lo_u)
    hi = _scale_amount(token_in, hi_u)

    # Evaluate function with cache (PS caches by (block, route, amount))
    async def eval_amt(amt_in: int) -> Optional[Dict[str, Any]]:
        if loop.time() >= deadline_s:
            return None
        c = {"route": route, "amount_in": int(amt_in)}
        if seed_payload.get("hops"):
            c["hops"] = seed_payload.get("hops")
        try:
            p = await asyncio.wait_for(
                PS.price_payload(c, block_ctx=block_ctx, fee_tiers=None, timeout_s=float(s.rpc_timeout_stage2_s)),
                timeout=float(s.rpc_timeout_stage2_s) + 0.5,
            )
            return p
        except Exception:
            return None

    # Start from seed and endpoints
    best = seed_payload
    evals = 0

    async def consider(p: Optional[Dict[str, Any]]):
        nonlocal best, evals
        if not p:
            return
        evals += 1
        if p.get("profit_raw", -1) > best.get("profit_raw", -10**30):
            best = p

    # Quick sanity: also test around seed amount
    seed_amt = int(seed_payload.get("amount_in", _scale_amount(token_in, s.stage1_amount)))
    seed_amt = max(lo, min(hi, seed_amt))

    # Golden-section style search with max_evals total
    phi = 0.61803398875
    a = lo
    b = hi
    c1 = int(b - (b - a) * phi)
    c2 = int(a + (b - a) * phi)

    # Always include seed point first (cheap if cached)
    await consider(await eval_amt(seed_amt))
    if loop.time() >= deadline_s:
        return best

    await consider(await eval_amt(c1))
    if loop.time() >= deadline_s or evals >= int(s.stage2_max_evals):
        return best
    await consider(await eval_amt(c2))
    if loop.time() >= deadline_s or evals >= int(s.stage2_max_evals):
        return best

    # Iterate
    while evals < int(s.stage2_max_evals) and (b - a) > max(10, int((hi - lo) * 0.01)):
        if loop.time() >= deadline_s:
            break

        # Compare profits at c1/c2 by reusing cached payloads
        p1 = await eval_amt(c1)
        p2 = await eval_amt(c2)
        await consider(p1)
        if evals >= int(s.stage2_max_evals) or loop.time() >= deadline_s:
            break
        await consider(p2)
        if evals >= int(s.stage2_max_evals) or loop.time() >= deadline_s:
            break

        pr1 = p1.get("profit_raw", -10**30) if p1 else -10**30
        pr2 = p2.get("profit_raw", -10**30) if p2 else -10**30

        if pr1 >= pr2:
            b = c2
            c2 = c1
            c1 = int(b - (b - a) * phi)
        else:
            a = c1
            c1 = c2
            c2 = int(a + (b - a) * phi)

    return best


async def simulate(payload: Dict[str, Any], block_number: int) -> None:
    # Provide structured fields for the web UI (spent, ROI, route),
    # so the table/stats never show "—" or "∞" because of parsing issues.
    try:
        route_t = tuple(payload.get("route") or ())
        token_in = route_t[0] if route_t else payload.get("token_in")
        dec = _decimals_by_token(str(token_in)) if token_in else 18
        spent_raw = int(payload.get("amount_in", 0) or 0)
        spent = float(spent_raw) / float(10 ** dec) if spent_raw else 0.0
    except Exception:
        spent = None

    profit = payload.get("profit_adj", payload.get("profit", 0.0))
    # Safety: if some code path mistakenly passes raw units,
    # convert to token units so UI doesn't show "∞".
    try:
        if isinstance(profit, (int, float)) and abs(float(profit)) > 1e9:
            pr = payload.get("profit_raw")
            if isinstance(pr, (int, float)) and spent is not None:
                # profit_raw is in smallest units of token_in
                route_t = tuple(payload.get("route") or ())
                token_in = route_t[0] if route_t else payload.get("token_in")
                dec = _decimals_by_token(str(token_in)) if token_in else 18
                profit = float(pr) / float(10 ** dec)
    except Exception:
        pass
    # Sanitize numbers early to avoid UI showing NaN/Infinity.
    profit = float(profit) if _is_finite_number(profit) else float("nan")
    profit = _clamp_sane(profit, abs_max=1_000_000_000.0)

    profit_pct = payload.get("profit_pct", None)
    token_sym = payload.get("token_in_symbol", "")
    route_str = _route_pretty(
        tuple(payload.get("route") or ()),
        payload.get("route_dex"),
        payload.get("route_fee_bps"),
        payload.get("route_fee_tier"),
        payload.get("dex_path"),
    )

    roi_pct = None
    try:
        if spent and spent > 0 and profit is not None:
            roi_pct = (float(profit) / float(spent)) * 100.0
        elif isinstance(profit_pct, (int, float)):
            roi_pct = float(profit_pct)
    except Exception:
        roi_pct = None

    # Compute ROI only if inputs are sane
    if spent is not None:
        spent = float(spent) if _is_finite_number(spent) else None
        if spent is not None:
            spent = _clamp_sane(spent, abs_max=1_000_000_000.0)

    if spent is None or spent <= 0 or not math.isfinite(profit):
        roi_pct = None
    else:
        try:
            roi_pct = (float(profit) / float(spent)) * 100.0
            if not math.isfinite(float(roi_pct)) or abs(float(roi_pct)) > 1e6:
                roi_pct = None
        except Exception:
            roi_pct = None

    # Don't emit profit events with nonsense values (NaN/inf or raw-unit explosions).
    if profit is None or not math.isfinite(float(profit)):
        await ui_push({
            "type": "warn",
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "block": block_number,
            "text": f"Dropped invalid profit payload (profit={profit}) route={route_str}",
        })
        return
    if abs(float(profit)) > 1_000_000_000.0:
        await ui_push({
            "type": "warn",
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "block": block_number,
            "text": f"Dropped implausibly large profit (unit mix?) profit={profit} route={route_str}",
        })
        return

    # UI-friendly rounding (doesn't change the economics, just presentation stability)
    profit = round(float(profit), 6)
    if spent is not None:
        spent = round(float(spent), 6)
    if roi_pct is not None:
        roi_pct = round(float(roi_pct), 2)

    await ui_push(
        {
            "type": "profit",
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "block": block_number,
            "profit": float(profit),
            "profit_symbol": str(token_sym),
            "spent": float(spent) if spent is not None else None,
            "spent_symbol": str(token_sym),
            "roi_pct": float(roi_pct) if roi_pct is not None else None,
            "route": route_str,
            "gas_units": int(payload.get("gas_units", 0) or 0),
            "text": f"profit={float(profit):.6f} {token_sym} spent={float(spent or 0):.6f} roi={float(roi_pct or 0):.4f}% route={route_str}",
        }
    )

    # Persist hit for debugging (JSONL)
    try:
        HIT_LOG.open("a", encoding="utf-8").write(json.dumps({
            "time": datetime.utcnow().isoformat(),
            "block": int(block_number),
            "route": route_str,
            "profit": float(profit),
            "spent": float(spent) if spent is not None else None,
            "roi_pct": float(roi_pct) if roi_pct is not None else None,
        }, ensure_ascii=False) + "\n")
    except Exception:
        pass
    print(
        f"[Executor] Simulating tx from {tx.get('from')} to {tx.get('to')} | Profit: {payload.get('profit',0):.6f}"
    )
    await asyncio.sleep(0)


def log(iteration: int, payloads: List[Dict[str, Any]], block_number: int) -> None:
    now = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{now}] Block {block_number} | Iteration {iteration} | Profitable payloads: {len(payloads)}")
    for p in payloads:
        route_t = tuple(p.get("route") or ())
        dec = _decimals_by_token(route_t[0]) if route_t else 18
        gross_raw = int(p.get("profit_gross_raw", p.get("profit_raw_no_gas", 0) or 0))
        net_raw = int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0))
        gas_raw = int(p.get("gas_cost", 0) or 0)
        gross = float(gross_raw) / float(10 ** dec)
        net = float(net_raw) / float(10 ** dec)
        gas = float(gas_raw) / float(10 ** dec)
        print(
            f"  Route: {_route_pretty(tuple(p.get('route')), p.get('route_dex'), p.get('route_fee_bps'), p.get('route_fee_tier'), p.get('dex_path'))} "
            f"| Gross: {gross:.6f} | Net: {net:.6f} | Gas: {gas:.6f}"
        )


def _read_jsonl_tail(path: Path, *, limit: int) -> List[Dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


async def replay_preflight(limit: int = 20) -> None:
    s = _load_settings()
    addr = str(getattr(s, "arb_executor_address", "") or "").strip()
    owner = str(getattr(s, "arb_executor_owner", "") or "").strip()
    if addr:
        config.ARB_EXECUTOR_ADDRESS = addr
    if owner:
        config.ARB_EXECUTOR_OWNER = owner
        config.SIM_FROM_ADDRESS = owner
    if not addr:
        raise SystemExit("arb_executor_address not set in bot_config.json")

    rpc_urls = list(s.rpc_urls) if getattr(s, "rpc_urls", None) else []
    if not rpc_urls:
        raw = os.getenv("RPC_HTTP_URLS") or os.getenv("RPC_URLS")
        if raw:
            rpc_urls = [x.strip() for x in raw.split(",") if x.strip()]
    if not rpc_urls:
        rpc_urls = [os.getenv("RPC_URL", config.RPC_URL)]

    rpc = RPCPool(rpc_urls, default_timeout_s=float(s.rpc_timeout_s))

    entries = _read_jsonl_tail(LOG_DIR / "trigger_scans.jsonl", limit=limit)
    ok_count = 0
    revert_count = 0
    errors: Dict[str, int] = {}

    try:
        for entry in entries:
            hops = entry.get("best_hops")
            hop_amounts = entry.get("best_hop_amounts")
            route = entry.get("best_route_tokens")
            if not (hops and hop_amounts and route):
                continue
            payload = {
                "route": route,
                "hops": hops,
                "hop_amounts": hop_amounts,
                "amount_in": entry.get("best_amount_in"),
                "candidate_id": entry.get("candidate_id"),
            }
            pre_block = entry.get("pre_block")
            block_tag = hex(int(pre_block)) if isinstance(pre_block, int) else "latest"
            try:
                calldata = preflight.build_arb_calldata(payload, s, to_addr=owner or config.SIM_FROM_ADDRESS)
                sim_ok, _, reason = await preflight.run_eth_call(
                    rpc,
                    block_tag=str(block_tag),
                    from_addr=str(owner or config.SIM_FROM_ADDRESS),
                    to_addr=str(addr),
                    data=calldata,
                    timeout_s=3.0,
                )
            except Exception as exc:
                sim_ok = False
                reason = f"error:{str(exc)[:120]}"

            if sim_ok:
                ok_count += 1
            else:
                revert_count += 1
                key = str(reason or "revert")
                errors[key] = int(errors.get(key, 0)) + 1
    finally:
        try:
            await rpc.close()
        except Exception:
            pass

    top_errors = sorted(errors.items(), key=lambda x: x[1], reverse=True)[:5]
    print(json.dumps({
        "entries_checked": len(entries),
        "ok_count": ok_count,
        "revert_count": revert_count,
        "top_revert_reasons": [{"reason": k, "count": v} for k, v in top_errors],
    }, indent=2))

async def main() -> None:
    iteration = 0
    print("🚀 Fork simulation started...")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": None, "text": "started"})
    seed_env = os.getenv("RUN_SEED")
    if seed_env:
        try:
            random.seed(int(seed_env))
        except Exception:
            pass

    # Init RPC endpoints (multi-RPC failover supported)
    global PS
    s0 = _load_settings()
    try:
        config.MEMPOOL_ALLOW_UNKNOWN_TOKENS = bool(getattr(s0, "mempool_allow_unknown_tokens", config.MEMPOOL_ALLOW_UNKNOWN_TOKENS))
        config.MEMPOOL_RAW_MIN_ENABLED = bool(getattr(s0, "mempool_raw_min_enabled", config.MEMPOOL_RAW_MIN_ENABLED))
        config.MEMPOOL_STRICT_UNKNOWN_TOKENS = bool(
            getattr(s0, "mempool_strict_unknown_tokens", config.MEMPOOL_STRICT_UNKNOWN_TOKENS)
        )
        config.TX_FETCH_BATCH_ENABLED = bool(getattr(s0, "tx_fetch_batch_enabled", config.TX_FETCH_BATCH_ENABLED))
        config.TX_FETCH_MAX_RETRIES = int(getattr(s0, "tx_fetch_max_retries", config.TX_FETCH_MAX_RETRIES))
        config.TX_FETCH_RETRY_BACKOFF_MS = list(getattr(s0, "tx_fetch_retry_backoff_ms", config.TX_FETCH_RETRY_BACKOFF_MS))
        config.TX_FETCH_PER_ENDPOINT_MAX_INFLIGHT = int(
            getattr(s0, "tx_fetch_per_endpoint_max_inflight", config.TX_FETCH_PER_ENDPOINT_MAX_INFLIGHT)
        )
        config.RPC_HEALTH_BAN_SECONDS = int(getattr(s0, "rpc_health_ban_seconds", config.RPC_HEALTH_BAN_SECONDS))
        config.RPC_BAN_SECONDS = int(getattr(s0, "rpc_health_ban_seconds", config.RPC_BAN_SECONDS))
        config.RPC_TIMEOUT_RATE_THRESHOLD = float(
            getattr(s0, "rpc_timeout_rate_threshold", config.RPC_TIMEOUT_RATE_THRESHOLD)
        )
        config.RPC_BAN_TIMEOUT_RATE = float(getattr(s0, "rpc_timeout_rate_threshold", config.RPC_BAN_TIMEOUT_RATE))
        config.RPC_LATENCY_P95_MS_THRESHOLD = float(
            getattr(s0, "rpc_latency_p95_ms_threshold", config.RPC_LATENCY_P95_MS_THRESHOLD)
        )
        config.RPC_BAN_LATENCY_P95_MS = float(
            getattr(s0, "rpc_latency_p95_ms_threshold", config.RPC_BAN_LATENCY_P95_MS)
        )
        config.RPC_OUT_OF_SYNC_BAN_SECONDS = int(
            getattr(s0, "out_of_sync_ban_seconds", config.RPC_OUT_OF_SYNC_BAN_SECONDS)
        )
        addr = str(getattr(s0, "arb_executor_address", "") or "").strip()
        if addr:
            config.ARB_EXECUTOR_ADDRESS = addr
        owner = str(getattr(s0, "arb_executor_owner", "") or "").strip()
        if owner:
            config.ARB_EXECUTOR_OWNER = owner
            config.SIM_FROM_ADDRESS = owner
    except Exception:
        pass
    rpc_urls = list(s0.rpc_urls) if getattr(s0, "rpc_urls", None) else []
    if not rpc_urls:
        # env RPC_HTTP_URLS / RPC_URLS fallback
        raw = os.getenv("RPC_HTTP_URLS") or os.getenv("RPC_URLS")
        if raw:
            rpc_urls = [x.strip() for x in raw.split(",") if x.strip()]
    if not rpc_urls:
        rpc_urls = [os.getenv("RPC_URL", config.RPC_URL)]

    enable_multidex = bool(getattr(s0, "enable_multidex", False)) or _env_flag("ENABLE_MULTIDEX")
    dexes = list(s0.dexes) if getattr(s0, "dexes", None) else None
    if not enable_multidex:
        dexes = ["univ3"]
    PS = scanner.PriceScanner(
        rpc_urls=rpc_urls,
        dexes=dexes,
        rpc_timeout_s=float(s0.rpc_timeout_s),
        rpc_retry_count=int(s0.rpc_retry_count),
    )
    dexes_used = list(getattr(PS, "dexes", []) or (dexes or []))

    scan_source = str(getattr(s0, "scan_source", "block") or "block").strip().lower()
    if scan_source not in ("block", "mempool", "hybrid"):
        scan_source = "block"
    mempool_on = bool(getattr(s0, "mempool_enabled", False)) or scan_source in ("mempool", "hybrid")
    block_on = scan_source in ("block", "hybrid")

    mempool_engine: Optional[MempoolEngine] = None

    session_started_at = datetime.utcnow()
    session_started_at_s = time.time()
    blocks_scanned = 0
    profit_hits = 0
    snapshotter: Optional[DiagnosticSnapshotter] = None
    stop_after_blocks = 0
    time_limit_s = 0.0
    try:
        stop_after_blocks = int(os.getenv("STOP_AFTER_BLOCKS", "0") or 0)
    except Exception:
        stop_after_blocks = 0
    try:
        time_limit_s = float(os.getenv("TIME_LIMIT_S", "0") or 0.0)
    except Exception:
        time_limit_s = 0.0
    run_started_s = time.time()
    blocks_seen = 0

    last_block = await PS.rpc.get_block_number()
    print(f"[RPC] connected=True | block={last_block} | urls={len(rpc_urls)}")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": int(last_block), "text": f"rpc connected=True | urls={len(rpc_urls)}"})
    global MEMPOOL_BLOCK_CTX
    pinned = None
    try:
        if hasattr(PS.rpc, "pick_pinned_endpoint"):
            pinned = PS.rpc.pick_pinned_endpoint()
    except Exception:
        pinned = None
    MEMPOOL_BLOCK_CTX = _make_block_context(int(last_block), pinned_http_endpoint=pinned)

    if mempool_on:
        ws_urls = list(getattr(s0, "mempool_ws_urls", ())) or list(getattr(config, "MEMPOOL_WS_URLS", []))
        if not ws_urls:
            raw_ws = os.getenv("RPC_WS_URLS")
            if raw_ws:
                ws_urls = [x.strip() for x in raw_ws.replace("\n", ",").split(",") if x.strip()]
        http_eps = list(getattr(s0, "rpc_http_endpoints", ()) or [])
        ws_eps = list(getattr(s0, "rpc_ws_endpoints", ()) or [])
        http_by_id = {str(ep.get("id")): str(ep.get("url")) for ep in http_eps if isinstance(ep, dict)}
        ws_by_id = {str(ep.get("id")): str(ep.get("url")) for ep in ws_eps if isinstance(ep, dict)}
        ws_pairing_cfg = getattr(s0, "rpc_ws_pairing", {}) or {}
        ws_http_pairing: Dict[str, str] = {}
        if isinstance(ws_pairing_cfg, dict):
            for ws_id, http_id in ws_pairing_cfg.items():
                ws_url = ws_by_id.get(str(ws_id)) or str(ws_id)
                http_url = http_by_id.get(str(http_id)) or str(http_id)
                if ws_url and http_url:
                    ws_http_pairing[ws_url] = http_url
        if not ws_http_pairing:
            for ws_id, ws_url in ws_by_id.items():
                http_url = http_by_id.get(ws_id)
                if ws_url and http_url:
                    ws_http_pairing[ws_url] = http_url
        if not ws_http_pairing:
            ws_hosts = {w.split("://", 1)[-1].split("/", 1)[0]: w for w in ws_urls}
            for hid, hurl in http_by_id.items():
                host = hurl.split("://", 1)[-1].split("/", 1)[0]
                if host in ws_hosts:
                    ws_http_pairing[ws_hosts[host]] = hurl

        if http_eps:
            tx_fetch_http_urls = [
                str(ep.get("url"))
                for ep in http_eps
                if isinstance(ep, dict)
                and ep.get("url")
                and "flashbots" not in str(ep.get("id", "")).lower()
                and "flashbots" not in str(ep.get("url", "")).lower()
            ]
        else:
            tx_fetch_http_urls = [u for u in rpc_urls if "flashbots" not in str(u).lower()]
        filter_to = list(getattr(s0, "mempool_filter_to", ())) or list(getattr(config, "MEMPOOL_FILTER_TO", []))
        watch_mode = getattr(s0, "mempool_watch_mode", getattr(config, "MEMPOOL_WATCH_MODE", "strict"))
        watched_router_set = getattr(s0, "mempool_watched_router_sets", getattr(config, "MEMPOOL_WATCHED_ROUTER_SETS", "core"))
        set_pinned_http_endpoint(None)
        mempool_engine = MempoolEngine(
            rpc=PS.rpc,
            ws_urls=ws_urls,
            http_urls=tx_fetch_http_urls,
            ws_http_pairing=ws_http_pairing,
            filter_to=filter_to,
            watch_mode=str(watch_mode),
            watched_router_set=str(watched_router_set),
            min_value_usd=float(getattr(s0, "mempool_min_value_usd", config.MEMPOOL_MIN_VALUE_USD)),
            usd_per_eth=float(getattr(s0, "mempool_usd_per_eth", config.MEMPOOL_USD_PER_ETH)),
            max_inflight=int(getattr(s0, "mempool_max_inflight_tx", config.MEMPOOL_MAX_INFLIGHT_TX)),
            fetch_concurrency=int(getattr(s0, "mempool_fetch_tx_concurrency", config.MEMPOOL_FETCH_TX_CONCURRENCY)),
            dedup_ttl_s=int(getattr(s0, "mempool_dedup_ttl_s", config.MEMPOOL_DEDUP_TTL_S)),
            trigger_budget_s=float(getattr(s0, "mempool_trigger_scan_budget_s", config.MEMPOOL_TRIGGER_SCAN_BUDGET_S)),
            trigger_prepare_budget_ms=int(
                getattr(s0, "trigger_prepare_budget_ms", getattr(config, "TRIGGER_PREPARE_BUDGET_MS", 250))
            ),
            trigger_queue_max=int(getattr(s0, "mempool_trigger_max_queue", config.MEMPOOL_TRIGGER_MAX_QUEUE)),
            trigger_concurrency=int(getattr(s0, "mempool_trigger_max_concurrent", config.MEMPOOL_TRIGGER_MAX_CONCURRENT)),
            trigger_ttl_s=int(getattr(s0, "mempool_trigger_ttl_s", config.MEMPOOL_TRIGGER_TTL_S)),
            confirm_timeout_s=float(getattr(s0, "mempool_confirm_timeout_s", config.MEMPOOL_CONFIRM_TIMEOUT_S)),
            post_scan_budget_s=float(getattr(s0, "mempool_post_scan_budget_s", config.MEMPOOL_POST_SCAN_BUDGET_S)),
            log_dir=LOG_DIR,
            ui_push=ui_push,
        )
        await mempool_engine.start(_scan_trigger)
        set_pinned_http_endpoint(pinned)
        await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": int(last_block), "text": f"mempool enabled ({scan_source})"})
        mempool_engine.set_block_number(int(last_block))
        token = set_pinned_http_endpoint(pinned)
        try:
            await asyncio.wait_for(PS.prepare_block(MEMPOOL_BLOCK_CTX), timeout=float(_prepare_budget_s(s0)))
        except Exception:
            pass
        finally:
            reset_pinned_http_endpoint(token)

    try:
        interval_s = float(os.getenv("DIAGNOSTIC_SNAPSHOT_INTERVAL_S", "") or 45.0)
    except Exception:
        interval_s = 45.0
    try:
        window_s = int(float(os.getenv("DIAGNOSTIC_SNAPSHOT_WINDOW_S", "") or 900))
    except Exception:
        window_s = 900
    snapshotter = DiagnosticSnapshotter(
        log_dir=LOG_DIR,
        get_settings=_load_settings,
        get_current_block=lambda: int(MEMPOOL_BLOCK_CTX.block_number) if MEMPOOL_BLOCK_CTX else None,
        session_started_at_s=session_started_at_s,
        rpc_provider=PS.rpc if PS else None,
        interval_s=interval_s,
        window_s=window_s,
    )
    snapshotter.write_snapshot(reason="startup")
    await snapshotter.start()

    try:
        while True:
            iteration += 1
            if time_limit_s > 0 and (time.time() - run_started_s) >= time_limit_s:
                break
            s = _load_settings()
            try:
                config.V2_MIN_RESERVE_RATIO = float(s.v2_min_reserve_ratio)
                config.V2_MAX_PRICE_IMPACT_BPS = float(s.v2_max_price_impact_bps)
            except Exception:
                pass
            try:
                config.MEMPOOL_ALLOW_UNKNOWN_TOKENS = bool(getattr(s, "mempool_allow_unknown_tokens", config.MEMPOOL_ALLOW_UNKNOWN_TOKENS))
                config.MEMPOOL_RAW_MIN_ENABLED = bool(getattr(s, "mempool_raw_min_enabled", config.MEMPOOL_RAW_MIN_ENABLED))
                addr = str(getattr(s, "arb_executor_address", "") or "").strip()
                if addr:
                    config.ARB_EXECUTOR_ADDRESS = addr
                owner = str(getattr(s, "arb_executor_owner", "") or "").strip()
                if owner:
                    config.ARB_EXECUTOR_OWNER = owner
                    config.SIM_FROM_ADDRESS = owner
            except Exception:
                pass

            # 0) wait new block
            block_number = await wait_for_new_block(last_block, timeout_s=float(s.rpc_timeout_s))
            last_block = block_number
            blocks_seen += 1
            print(f"Block {block_number}")
            pinned = None
            try:
                if hasattr(PS.rpc, "pick_pinned_endpoint"):
                    pinned = PS.rpc.pick_pinned_endpoint()
            except Exception:
                pinned = None
            block_ctx = _make_block_context(int(block_number), pinned_http_endpoint=pinned)
            MEMPOOL_BLOCK_CTX = block_ctx
            if mempool_engine:
                mempool_engine.set_block_number(int(block_number))

            set_pinned_http_endpoint(pinned)
            # Optional gas gate
            if s.max_gas_gwei is not None:
                gas_gwei = await _gas_gwei(timeout_s=float(s.rpc_timeout_s))
                if gas_gwei is not None and gas_gwei > float(s.max_gas_gwei):
                    METRICS.inc_reason("drop_reason_counts", "gas_too_high", 1)
                    await ui_push(
                        {
                            "type": "scan",
                            "time": datetime.utcnow().strftime("%H:%M:%S"),
                            "block": block_number,
                            "candidates": 0,
                            "profitable": 0,
                            "best_profit": 0.0,
                            "text": f"Skipped: gas {gas_gwei:.1f} gwei > max {float(s.max_gas_gwei):.1f} gwei",
                        }
                    )
                    await _confirm_mempool(mempool_engine, int(block_number))
                    continue

            if not block_on:
                # In mempool-only mode we still refresh caches + confirm triggers.
                try:
                    await asyncio.wait_for(PS.prepare_block(block_ctx), timeout=float(_prepare_budget_s(s)))
                except Exception:
                    pass
                if mempool_engine:
                    await mempool_engine.confirm_block(int(block_number))
                await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": f"mempool mode block {block_number}"})
                if stop_after_blocks > 0 and blocks_seen >= stop_after_blocks:
                    break
                continue

            await ui_push({"type": "scan", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": "Scanning routes..."})

            routes = await scan_routes()
            if not routes:
                await ui_push(
                    {
                        "type": "scan",
                        "time": datetime.utcnow().strftime("%H:%M:%S"),
                        "block": block_number,
                        "candidates": 0,
                        "profitable": 0,
                        "best_profit": 0.0,
                        "text": "No routes generated",
                    }
                )
                blocks_scanned += 1
                _write_session_summary(
                    started_at=session_started_at,
                    updated_at=datetime.utcnow(),
                    duration_s=(datetime.utcnow() - session_started_at).total_seconds(),
                    rpc_urls=rpc_urls,
                    dexes=dexes_used,
                    blocks_scanned=blocks_scanned,
                    profit_hits=profit_hits,
                    settings=s,
                    env_flags={
                        "SIM_PROFILE": str(os.getenv("SIM_PROFILE", "")),
                        "DEBUG_FUNNEL": "1" if _env_flag("DEBUG_FUNNEL") else "0",
                        "GAS_OFF": "1" if _env_flag("GAS_OFF") else "0",
                        "FIXED_GAS_UNITS": str(os.getenv("FIXED_GAS_UNITS", "")),
                        "ENABLE_MULTIDEX": "1" if enable_multidex else "0",
                        "SCAN_SOURCE": str(scan_source),
                        "MEMPOOL_ENABLED": "1" if mempool_on else "0",
                    },
                )
                await _confirm_mempool(mempool_engine, int(block_number))
                continue

            attempt_ctxs = [block_ctx]
            if int(block_number) > 0:
                fallback_pin = None
                try:
                    if hasattr(PS.rpc, "pick_pinned_endpoint"):
                        fallback_pin = PS.rpc.pick_pinned_endpoint()
                except Exception:
                    fallback_pin = None
                attempt_ctxs.append(_make_block_context(int(block_number) - 1, pinned_http_endpoint=fallback_pin))

            scan_done = False
            block_ctx = attempt_ctxs[0]
            for attempt_idx, ctx in enumerate(attempt_ctxs):
                block_ctx = ctx
                set_pinned_http_endpoint(getattr(block_ctx, "pinned_http_endpoint", None))
                loop = asyncio.get_running_loop()
                block_start_t = loop.time()
                prepare_budget_s = _prepare_budget_s(s)
                prepare_start_t = loop.time()
                prepare_over_budget = False

                # prepare per-block caches (gas + WETH/USDC warmup)
                try:
                    await asyncio.wait_for(PS.prepare_block(block_ctx), timeout=float(prepare_budget_s))
                except asyncio.TimeoutError:
                    prepare_over_budget = True
                except Exception:
                    pass

                prepare_ms = (loop.time() - prepare_start_t) * 1000.0
                if prepare_ms / 1000.0 > float(prepare_budget_s):
                    prepare_over_budget = True

                scan_start_t = loop.time()
                scan_start_delay_ms = int(max(0.0, (scan_start_t - block_start_t) * 1000.0))
                scan_budget_s = _compute_scan_budget_s(float(s.block_budget_s), scan_start_t - block_start_t, min_scan_s=1.0)
                global_deadline = scan_start_t + float(scan_budget_s)
                min_scan_reserve_s = float(getattr(s, "min_scan_reserve_s", 0.6))

                cap_ratio = 0.5 if prepare_over_budget else 1.0
                max_candidates_stage1 = _cap_int(int(getattr(s, "max_candidates_stage1", 200)))
                max_total_expanded = _cap_int(int(getattr(s, "max_total_expanded", 400)))
                max_per_candidate = _cap_int(int(getattr(s, "max_expanded_per_candidate", 6)))
                if cap_ratio < 1.0:
                    max_candidates_stage1 = max(1, int(max_candidates_stage1 * cap_ratio))
                    max_total_expanded = max(1, int(max_total_expanded * cap_ratio))
                    max_per_candidate = max(1, int(max_per_candidate * cap_ratio))

                routes_for_expand = list(routes)
                if enable_multidex:
                    max_routes_for_expand = max(1, int(max_total_expanded / max(1, max_per_candidate)))
                    if len(routes_for_expand) > max_routes_for_expand:
                        routes_for_expand = routes_for_expand[: max_routes_for_expand]
                cand_estimate = len(routes_for_expand)
                if enable_multidex:
                    cand_estimate = min(int(len(routes_for_expand) * max_per_candidate), int(max_total_expanded))
                cand_estimate = min(int(cand_estimate), int(max_candidates_stage1))
                stage1_ratio = _adaptive_stage1_ratio(int(cand_estimate), _rpc_latency_ms())
                stage1_window_s = float(scan_budget_s) * float(stage1_ratio)
                if stage1_window_s < min_scan_reserve_s:
                    stage1_window_s = float(min_scan_reserve_s)
                stage1_deadline = min(global_deadline, scan_start_t + stage1_window_s)
                expand_window_s = _expand_budget_s(stage1_window_s, s)
                expand_deadline = scan_start_t + expand_window_s
                hard_scan_deadline = max(scan_start_t, stage1_deadline - float(min_scan_reserve_s))
                if expand_deadline > hard_scan_deadline:
                    expand_deadline = hard_scan_deadline
                if expand_deadline < scan_start_t:
                    expand_deadline = scan_start_t
                deadline = global_deadline
                stage1_deadline_remaining_ms_at_scan_start = int(max(0.0, (stage1_deadline - scan_start_t) * 1000.0))
                stage1_remaining_ms_at_scan_start = int(max(0.0, (stage1_deadline - scan_start_t) * 1000.0))

                profitable: List[Dict[str, Any]] = []
                candidates_count = 0
                finished_count = 0
                funnel_payloads: List[Dict[str, Any]] = []
                funnel_counts = {"raw": 0, "gas": 0, "safety": 0, "ready": 0}
                funnel_stats: Dict[str, Optional[float]] = {}
                top_examples: Dict[str, List[Dict[str, Any]]] = {}
                scan_stats: Dict[str, Any] = {
                    "scheduled": 0,
                    "finished": 0,
                    "timeouts": 0,
                    "invalid": 0,
                    "budget_skipped": 0,
                    "sanity_rejects_total": 0,
                    "rejects_by_reason": {},
                    "expand_ms": 0,
                    "beam_ms": 0,
                    "scan_schedule_ms": 0,
                    "scan_await_ms": 0,
                    "stage1_remaining_ms_at_scan_start": int(stage1_remaining_ms_at_scan_start),
                    "stage1_remaining_ms_before_scheduling": 0,
                    "candidates_initial": int(len(routes)),
                    "candidates_after_beam": 0,
                    "candidates_after_multidex": 0,
                    "capped_by_max_candidates_stage1": False,
                    "capped_by_max_total_expanded": False,
                    "capped_by_max_expanded_per_candidate": False,
                    "viability_fallback_used": False,
                    "beam_fallback_used": False,
                }
                if enable_multidex and len(routes_for_expand) != len(routes):
                    scan_stats["routes_pruned_for_expand"] = int(len(routes) - len(routes_for_expand))
                    scan_stats["routes_for_expand"] = int(len(routes_for_expand))
                scan_stats["prepare_ms"] = int(max(0.0, prepare_ms))
                scan_stats["scan_start_delay_ms"] = int(max(0, scan_start_delay_ms))
                scan_stats["stage1_deadline_remaining_ms_at_scan_start"] = int(stage1_deadline_remaining_ms_at_scan_start)
                scan_stats["prepare_over_budget"] = bool(prepare_over_budget)

                try:
                    if str(s.scan_mode).lower() == "fixed":
                        candidates = []
                        viability_cache: Optional[Dict[Tuple[str, str, str, int], Optional[int]]] = (
                            {} if getattr(config, "VIABILITY_CACHE_ENABLED", True) else None
                        )
                        if enable_multidex:
                            base_token = routes[0][0]
                            remaining_total = int(max_total_expanded)
                            expand_stats: Dict[str, Any] = {}
                            for u in list(s.amount_presets):
                                if remaining_total <= 0:
                                    break
                                amt_in = _scale_amount(base_token, u)
                                more = await _expand_multidex_candidates(
                                    routes_for_expand,
                                    amount_in=int(amt_in),
                                    block_ctx=block_ctx,
                                    fee_tiers=None,
                                    timeout_s=float(s.rpc_timeout_stage2_s),
                                    deadline_s=expand_deadline,
                                    settings=s,
                                    max_total=int(remaining_total),
                                    max_per_route=int(max_per_candidate),
                                    viability_cache=viability_cache,
                                    stats=expand_stats,
                                )
                                if more:
                                    candidates.extend(more)
                                    remaining_total = int(max_total_expanded) - len(candidates)
                            if not candidates and routes:
                                # Fallback: skip viability probes to avoid empty candidate sets.
                                fallback_routes = routes[: max(1, min(int(max_candidates_stage1), 40))]
                                fallback_total = min(int(max_total_expanded), int(max_candidates_stage1), 40)
                                fallback_per = min(int(max_per_candidate), 2)
                                amt_in = _scale_amount(base_token, s.stage1_amount)
                                fallback_deadline = min(stage1_deadline, loop.time() + 1.5)
                                more = await _expand_multidex_candidates(
                                    fallback_routes,
                                    amount_in=int(amt_in),
                                    block_ctx=block_ctx,
                                    fee_tiers=None,
                                    timeout_s=min(float(s.rpc_timeout_stage2_s), 1.2),
                                    deadline_s=fallback_deadline,
                                    settings=s,
                                    max_total=int(fallback_total),
                                    max_per_route=int(fallback_per),
                                    viability_cache=viability_cache,
                                    stats=expand_stats,
                                    skip_viability=True,
                                )
                                if more:
                                    candidates.extend(more)
                                    scan_stats["viability_fallback_used"] = True
                            if not candidates and routes_for_expand:
                                # Final fallback: legacy token-only candidates (still multi-dex via best_edge).
                                for route in routes_for_expand:
                                    token_in = route[0]
                                    for u in list(s.amount_presets):
                                        candidates.append({"route": route, "amount_in": _scale_amount(token_in, u)})
                                        if len(candidates) >= int(max_candidates_stage1):
                                            break
                                    if len(candidates) >= int(max_candidates_stage1):
                                        break
                                if candidates:
                                    scan_stats["beam_fallback_used"] = True
                            if expand_stats:
                                scan_stats["expand_ms"] = float(expand_stats.get("expand_ms", 0.0))
                                scan_stats["beam_ms"] = float(expand_stats.get("beam_ms", 0.0))
                                scan_stats["candidates_after_beam"] = int(expand_stats.get("candidates_after_beam", 0))
                                scan_stats["candidates_after_multidex"] = int(expand_stats.get("candidates_after_multidex", 0))
                                scan_stats["capped_by_max_total_expanded"] = bool(expand_stats.get("capped_by_max_total_expanded", False))
                                scan_stats["capped_by_max_expanded_per_candidate"] = bool(expand_stats.get("capped_by_max_expanded_per_candidate", False))
                        else:
                            for route in routes:
                                token_in = route[0]
                                for u in list(s.amount_presets):
                                    candidates.append({"route": route, "amount_in": _scale_amount(token_in, u)})
                                    if len(candidates) >= int(max_candidates_stage1):
                                        break
                                if len(candidates) >= int(max_candidates_stage1):
                                    break
                            scan_stats["candidates_after_beam"] = int(len(candidates))
                            scan_stats["candidates_after_multidex"] = int(len(candidates))
                        if len(candidates) > int(max_candidates_stage1):
                            candidates = candidates[: int(max_candidates_stage1)]
                            scan_stats["capped_by_max_candidates_stage1"] = True
                        scan_stats["candidates_after_multidex"] = int(len(candidates))
                        candidates_count = len(candidates)
                        scan_start_delay_ms = int(max(0.0, (loop.time() - block_start_t) * 1000.0))
                        stage1_deadline_remaining_ms_at_scan_start = int(
                            max(0.0, (stage1_deadline - loop.time()) * 1000.0)
                        )
                        payloads, finished_count, scan_stats_run = await _scan_candidates(
                            candidates,
                            block_ctx,
                            fee_tiers=None,
                            timeout_s=float(s.rpc_timeout_stage2_s),
                            deadline_s=deadline,
                            keep_all=True,
                        )
                        scan_stats.update(scan_stats_run)
                        scan_stats["prepare_ms"] = int(max(0.0, prepare_ms))
                        scan_stats["scan_start_delay_ms"] = int(max(0, scan_start_delay_ms))
                        scan_stats["stage1_deadline_remaining_ms_at_scan_start"] = int(
                            max(0, stage1_deadline_remaining_ms_at_scan_start)
                        )
                        scan_stats["stage1_remaining_ms_at_scan_start"] = int(max(0, stage1_remaining_ms_at_scan_start))
                        scan_stats["prepare_over_budget"] = bool(prepare_over_budget)
                        payloads = [p for p in payloads if p and p.get("profit_raw", -1) != -1]
                        payloads2 = [_apply_safety(p, s) for p in payloads]
                        funnel_payloads = payloads2
                        funnel_counts = _funnel_counts(funnel_payloads, s)
                        funnel_stats = _funnel_stats(funnel_payloads)
                        top_examples = _top_examples(funnel_payloads)
                        for p2 in payloads2:
                            if not strategies.risk_check(p2):
                                METRICS.inc_reason("drop_reason_counts", "illiquid", 1)
                                continue
                            if not _passes_thresholds(p2, s):
                                reason = "min_profit_not_met"
                                try:
                                    if float(p2.get("profit_adj", 0.0)) <= 0:
                                        reason = "slippage_fail"
                                except Exception:
                                    pass
                                METRICS.inc_reason("drop_reason_counts", reason, 1)
                                continue
                            profitable.append(p2)

                    else:
                        # Stage 1
                        viability_cache = {} if getattr(config, "VIABILITY_CACHE_ENABLED", True) else None
                        if enable_multidex:
                            base_token = routes[0][0]
                            stage1_amount = _scale_amount(base_token, s.stage1_amount)
                            expand_stats: Dict[str, Any] = {}
                            stage1_candidates = await _expand_multidex_candidates(
                                routes_for_expand,
                                amount_in=int(stage1_amount),
                                block_ctx=block_ctx,
                                fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
                                timeout_s=float(s.rpc_timeout_stage1_s),
                                deadline_s=expand_deadline,
                                settings=s,
                                max_total=int(max_total_expanded),
                                max_per_route=int(max_per_candidate),
                                viability_cache=viability_cache,
                                stats=expand_stats,
                            )
                            if not stage1_candidates and routes:
                                # Fallback: skip viability probes to avoid empty candidate sets.
                                fallback_routes = routes[: max(1, min(int(max_candidates_stage1), 40))]
                                fallback_total = min(int(max_total_expanded), int(max_candidates_stage1), 40)
                                fallback_per = min(int(max_per_candidate), 2)
                                fallback_deadline = min(stage1_deadline, loop.time() + 1.5)
                                stage1_candidates = await _expand_multidex_candidates(
                                    fallback_routes,
                                    amount_in=int(stage1_amount),
                                    block_ctx=block_ctx,
                                    fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
                                    timeout_s=min(float(s.rpc_timeout_stage1_s), 1.2),
                                    deadline_s=fallback_deadline,
                                    settings=s,
                                    max_total=int(fallback_total),
                                    max_per_route=int(fallback_per),
                                    viability_cache=viability_cache,
                                    stats=expand_stats,
                                    skip_viability=True,
                                )
                                if stage1_candidates:
                                    scan_stats["viability_fallback_used"] = True
                            if not stage1_candidates and routes_for_expand:
                                # Final fallback: legacy token-only candidates (still multi-dex via best_edge).
                                stage1_candidates = [
                                    {"route": route, "amount_in": _scale_amount(route[0], s.stage1_amount)}
                                    for route in routes_for_expand[: int(max_candidates_stage1)]
                                ]
                                if stage1_candidates:
                                    scan_stats["beam_fallback_used"] = True
                            if expand_stats:
                                scan_stats["expand_ms"] = float(expand_stats.get("expand_ms", 0.0))
                                scan_stats["beam_ms"] = float(expand_stats.get("beam_ms", 0.0))
                                scan_stats["candidates_after_beam"] = int(expand_stats.get("candidates_after_beam", 0))
                                scan_stats["candidates_after_multidex"] = int(expand_stats.get("candidates_after_multidex", 0))
                                scan_stats["capped_by_max_total_expanded"] = bool(expand_stats.get("capped_by_max_total_expanded", False))
                                scan_stats["capped_by_max_expanded_per_candidate"] = bool(expand_stats.get("capped_by_max_expanded_per_candidate", False))
                        else:
                            stage1_candidates = [
                                {"route": route, "amount_in": _scale_amount(route[0], s.stage1_amount)} for route in routes
                            ]
                            scan_stats["candidates_after_beam"] = int(len(stage1_candidates))
                            scan_stats["candidates_after_multidex"] = int(len(stage1_candidates))
                        if len(stage1_candidates) > int(max_candidates_stage1):
                            stage1_candidates = stage1_candidates[: int(max_candidates_stage1)]
                            scan_stats["capped_by_max_candidates_stage1"] = True
                        scan_stats["candidates_after_multidex"] = int(len(stage1_candidates))
                        candidates_count = len(stage1_candidates)

                        scan_start_delay_ms = int(max(0.0, (loop.time() - block_start_t) * 1000.0))
                        stage1_deadline_remaining_ms_at_scan_start = int(
                            max(0.0, (stage1_deadline - loop.time()) * 1000.0)
                        )
                        stage1_payloads, finished_count, scan_stats_run = await _scan_candidates(
                            stage1_candidates,
                            block_ctx,
                            fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
                            timeout_s=float(s.rpc_timeout_stage1_s),
                            deadline_s=stage1_deadline,
                            keep_all=True,
                        )
                        scan_stats.update(scan_stats_run)
                        scan_stats["prepare_ms"] = int(max(0.0, prepare_ms))
                        scan_stats["scan_start_delay_ms"] = int(max(0, scan_start_delay_ms))
                        scan_stats["stage1_deadline_remaining_ms_at_scan_start"] = int(
                            max(0, stage1_deadline_remaining_ms_at_scan_start)
                        )
                        scan_stats["stage1_remaining_ms_at_scan_start"] = int(max(0, stage1_remaining_ms_at_scan_start))
                        scan_stats["prepare_over_budget"] = bool(prepare_over_budget)

                        stage1_payloads = [p for p in stage1_payloads if p and p.get("profit_raw", -1) != -1]
                        stage1_payloads2 = [_apply_safety(p, s) for p in stage1_payloads]
                        funnel_payloads = stage1_payloads2
                        funnel_counts = _funnel_counts(funnel_payloads, s)
                        funnel_stats = _funnel_stats(funnel_payloads)
                        top_examples = _top_examples(funnel_payloads)
                        ranked = sorted(stage1_payloads, key=lambda p: int(p.get("profit_raw", -10**30)), reverse=True)

                        # Soft filter to avoid missing profit: keep positives, or near-threshold
                        soft: List[Dict[str, Any]] = []
                        for p in ranked:
                            if float(p.get("profit", 0.0)) > 0:
                                soft.append(p)
                            elif float(p.get("profit_pct", 0.0)) >= max(0.0, float(s.min_profit_pct) * 0.25):
                                soft.append(p)
                            elif float(p.get("profit", 0.0)) >= max(0.0, float(s.min_profit_abs) * 0.25):
                                soft.append(p)
                        if not soft:
                            soft = ranked

                        stage2_top_k = int(s.stage2_top_k)
                        if prepare_over_budget:
                            stage2_top_k = max(5, int(stage2_top_k * 0.7))
                        topk = soft[: int(stage2_top_k)]
                        sem2 = asyncio.Semaphore(max(1, min(int(s.concurrency), 12)))

                        async def opt_one(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                            async with sem2:
                                return await _optimize_amount_for_route(tuple(p.get("route")), block_ctx, p, deadline_s=deadline)

                        opt_tasks = [asyncio.create_task(opt_one(p)) for p in topk]
                        best_payloads: List[Dict[str, Any]] = []
                        try:
                            pending = set(opt_tasks)
                            while pending and loop.time() < deadline:
                                timeout = max(0.0, float(deadline) - float(loop.time()))
                                done, pending = await asyncio.wait(
                                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                                )
                                for d in done:
                                    bp = await d
                                    if bp:
                                        best_payloads.append(bp)
                        finally:
                            for t in opt_tasks:
                                if not t.done():
                                    t.cancel()
                            await asyncio.gather(*opt_tasks, return_exceptions=True)

                        best_payloads = [bp for bp in best_payloads if bp and bp.get("profit_raw", -1) != -1]
                        best_payloads2 = [_apply_safety(bp, s) for bp in best_payloads]
                        for bp2 in best_payloads2:
                            if not strategies.risk_check(bp2):
                                METRICS.inc_reason("drop_reason_counts", "illiquid", 1)
                                continue
                            if not _passes_thresholds(bp2, s):
                                reason = "min_profit_not_met"
                                try:
                                    if float(bp2.get("profit_adj", 0.0)) <= 0:
                                        reason = "slippage_fail"
                                except Exception:
                                    pass
                                METRICS.inc_reason("drop_reason_counts", reason, 1)
                                continue
                            profitable.append(bp2)

                except Exception as e:
                    print(f"[WARN] scan failed: {type(e).__name__}: {e}")
                    await ui_push({"type": "warn", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": int(block_ctx.block_number), "text": f"scan failed: {type(e).__name__}"})
                    await asyncio.sleep(0.5)
                    break

                if attempt_idx == 0 and _should_fallback_block(scan_stats, candidates_count=candidates_count):
                    msg = f"RPC not caught up for block {block_ctx.block_number}, retrying {int(block_ctx.block_number) - 1}"
                    print(f"[WARN] {msg}")
                    METRICS.inc("out_of_sync_count", 1)
                    METRICS.inc("rpc_not_caught_up_events", 1)
                    try:
                        if hasattr(PS.rpc, "mark_out_of_sync"):
                            PS.rpc.mark_out_of_sync(getattr(block_ctx, "pinned_http_endpoint", None))
                    except Exception:
                        pass
                    await ui_push({"type": "warn", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": int(block_ctx.block_number), "text": msg})
                    continue

                _debug_funnel_log(funnel_payloads, s, int(block_ctx.block_number), scan_stats)
                scan_done = True
                break

            if not scan_done:
                await _confirm_mempool(mempool_engine, int(block_ctx.block_number))
                continue

            try:
                METRICS.inc("candidates_generated", int(candidates_count))
                scheduled = int(scan_stats.get("scheduled", 0))
                invalid = int(scan_stats.get("invalid", 0))
                METRICS.inc("candidates_after_probe", max(0, scheduled - invalid))
                METRICS.inc("candidates_after_refine", int(len(funnel_payloads)))
                if scheduled == 0:
                    reason = scan_stats.get("reason_if_zero_scheduled")
                    METRICS.inc_reason("drop_reason_counts", _normalize_drop_reason(reason), 1)
            except Exception:
                pass

            if not profitable:
                await ui_push(
                    {
                        "type": "scan",
                        "time": datetime.utcnow().strftime("%H:%M:%S"),
                        "block": int(block_ctx.block_number),
                        "candidates": int(candidates_count),
                        "profitable": 0,
                        "best_profit": 0.0,
                        "raw_opps": int(funnel_counts["raw"]),
                        "safety_opps": int(funnel_counts["safety"]),
                        "gas_opps": int(funnel_counts["gas"]),
                        "final_opps": int(funnel_counts["ready"]),
                        "prepare_ms": scan_stats.get("prepare_ms"),
                        "scan_start_delay_ms": scan_stats.get("scan_start_delay_ms"),
                        "stage1_deadline_remaining_ms_at_scan_start": scan_stats.get("stage1_deadline_remaining_ms_at_scan_start"),
                        "stage1_remaining_ms_at_scan_start": scan_stats.get("stage1_remaining_ms_at_scan_start"),
                        "stage1_remaining_ms_before_scheduling": scan_stats.get("stage1_remaining_ms_before_scheduling"),
                        "scan_schedule_ms": scan_stats.get("scan_schedule_ms"),
                        "scan_await_ms": scan_stats.get("scan_await_ms"),
                        "expand_ms": scan_stats.get("expand_ms"),
                        "beam_ms": scan_stats.get("beam_ms"),
                        "candidates_initial": scan_stats.get("candidates_initial"),
                        "routes_for_expand": scan_stats.get("routes_for_expand"),
                        "routes_pruned_for_expand": scan_stats.get("routes_pruned_for_expand"),
                        "candidates_after_beam": scan_stats.get("candidates_after_beam"),
                        "candidates_after_multidex": scan_stats.get("candidates_after_multidex"),
                        "capped_by_max_candidates_stage1": scan_stats.get("capped_by_max_candidates_stage1"),
                        "capped_by_max_total_expanded": scan_stats.get("capped_by_max_total_expanded"),
                        "capped_by_max_expanded_per_candidate": scan_stats.get("capped_by_max_expanded_per_candidate"),
                        "viability_fallback_used": scan_stats.get("viability_fallback_used"),
                        "beam_fallback_used": scan_stats.get("beam_fallback_used"),
                        "reason_if_zero_scheduled": scan_stats.get("reason_if_zero_scheduled"),
                        "sanity_rejects_total": scan_stats.get("sanity_rejects_total"),
                        "rejects_by_reason": scan_stats.get("rejects_by_reason"),
                        "text": (
                            f"Scanned {int(finished_count)}/{int(candidates_count)} routes | "
                            f"raw={int(funnel_counts['raw'])} gas={int(funnel_counts['gas'])} "
                            f"safety={int(funnel_counts['safety'])} ready={int(funnel_counts['ready'])} | mode={s.scan_mode}"
                        ),
                    }
                )
                blocks_scanned += 1
                # Emit RPC pool stats + block summary for debugging
                try:
                    if hasattr(PS.rpc, "stats"):
                        await ui_push({
                            "type": "rpc_stats",
                            "time": datetime.utcnow().strftime("%H:%M:%S"),
                            "block": int(block_ctx.block_number),
                            "stats": PS.rpc.stats(),
                        })
                except Exception:
                    pass
                try:
                    BLOCK_LOG.open("a", encoding="utf-8").write(json.dumps({
                        "time": datetime.utcnow().isoformat(),
                        "block": int(block_ctx.block_number),
                        "candidates": int(candidates_count),
                        "candidates_total": int(candidates_count),
                        "finished": int(finished_count),
                        "scan_scheduled": int(scan_stats.get("scheduled", 0)),
                        "scan_invalid": int(scan_stats.get("invalid", 0)),
                        "scan_timeouts": int(scan_stats.get("timeouts", 0)),
                        "scan_budget_skipped": int(scan_stats.get("budget_skipped", 0)),
                        "prepare_ms": scan_stats.get("prepare_ms"),
                        "scan_start_delay_ms": scan_stats.get("scan_start_delay_ms"),
                        "stage1_deadline_remaining_ms_at_scan_start": scan_stats.get("stage1_deadline_remaining_ms_at_scan_start"),
                        "stage1_remaining_ms_at_scan_start": scan_stats.get("stage1_remaining_ms_at_scan_start"),
                        "stage1_remaining_ms_before_scheduling": scan_stats.get("stage1_remaining_ms_before_scheduling"),
                        "scan_schedule_ms": scan_stats.get("scan_schedule_ms"),
                        "scan_await_ms": scan_stats.get("scan_await_ms"),
                        "expand_ms": scan_stats.get("expand_ms"),
                        "beam_ms": scan_stats.get("beam_ms"),
                        "candidates_initial": scan_stats.get("candidates_initial"),
                        "routes_for_expand": scan_stats.get("routes_for_expand"),
                        "routes_pruned_for_expand": scan_stats.get("routes_pruned_for_expand"),
                        "candidates_after_beam": scan_stats.get("candidates_after_beam"),
                        "candidates_after_multidex": scan_stats.get("candidates_after_multidex"),
                        "capped_by_max_candidates_stage1": scan_stats.get("capped_by_max_candidates_stage1"),
                        "capped_by_max_total_expanded": scan_stats.get("capped_by_max_total_expanded"),
                        "capped_by_max_expanded_per_candidate": scan_stats.get("capped_by_max_expanded_per_candidate"),
                        "viability_fallback_used": scan_stats.get("viability_fallback_used"),
                        "beam_fallback_used": scan_stats.get("beam_fallback_used"),
                        "reason_if_zero_scheduled": scan_stats.get("reason_if_zero_scheduled"),
                        "sanity_rejects_total": scan_stats.get("sanity_rejects_total"),
                        "rejects_by_reason": scan_stats.get("rejects_by_reason"),
                        "profitable": 0,
                        "raw_opps": int(funnel_counts["raw"]),
                        "raw_gross_hits": int(funnel_counts["raw"]),
                        "safety_opps": int(funnel_counts["safety"]),
                        "safety_hits": int(funnel_counts["safety"]),
                        "gas_opps": int(funnel_counts["gas"]),
                        "net_hits": int(funnel_counts["gas"]),
                        "final_opps": int(funnel_counts["ready"]),
                        "ready_hits": int(funnel_counts["ready"]),
                        "top_examples": top_examples,
                        "profit_gross_min": funnel_stats.get("gross_min"),
                        "profit_gross_max": funnel_stats.get("gross_max"),
                        "profit_net_min": funnel_stats.get("net_min"),
                        "profit_net_max": funnel_stats.get("net_max"),
                        "gas_units_min": funnel_stats.get("gas_units_min"),
                        "gas_units_max": funnel_stats.get("gas_units_max"),
                        "gas_units_avg": funnel_stats.get("gas_units_avg"),
                        "gas_units_median": funnel_stats.get("gas_units_median"),
                        "gas_cost_min": funnel_stats.get("gas_cost_min"),
                        "gas_cost_max": funnel_stats.get("gas_cost_max"),
                        "gas_cost_avg": funnel_stats.get("gas_cost_avg"),
                        "gas_cost_median": funnel_stats.get("gas_cost_median"),
                        "mode": str(s.scan_mode),
                    }, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                _write_session_summary(
                    started_at=session_started_at,
                    updated_at=datetime.utcnow(),
                    duration_s=(datetime.utcnow() - session_started_at).total_seconds(),
                    rpc_urls=rpc_urls,
                    dexes=dexes_used,
                    blocks_scanned=blocks_scanned,
                    profit_hits=profit_hits,
                    settings=s,
                    env_flags={
                        "SIM_PROFILE": str(os.getenv("SIM_PROFILE", "")),
                        "DEBUG_FUNNEL": "1" if _env_flag("DEBUG_FUNNEL") else "0",
                        "GAS_OFF": "1" if _env_flag("GAS_OFF") else "0",
                        "FIXED_GAS_UNITS": str(os.getenv("FIXED_GAS_UNITS", "")),
                        "ENABLE_MULTIDEX": "1" if enable_multidex else "0",
                        "SCAN_SOURCE": str(scan_source),
                        "MEMPOOL_ENABLED": "1" if mempool_on else "0",
                    },
                )
                await _confirm_mempool(mempool_engine, int(block_ctx.block_number))
                continue

            best_profit = max((float(p.get("profit_adj", p.get("profit", 0))) for p in profitable), default=0.0)
            await ui_push(
                {
                    "type": "scan",
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "block": int(block_ctx.block_number),
                    "candidates": int(candidates_count),
                    "profitable": len(profitable),
                    "best_profit": float(best_profit),
                    "raw_opps": int(funnel_counts["raw"]),
                    "safety_opps": int(funnel_counts["safety"]),
                    "gas_opps": int(funnel_counts["gas"]),
                    "final_opps": int(funnel_counts["ready"]),
                    "prepare_ms": scan_stats.get("prepare_ms"),
                    "scan_start_delay_ms": scan_stats.get("scan_start_delay_ms"),
                    "stage1_deadline_remaining_ms_at_scan_start": scan_stats.get("stage1_deadline_remaining_ms_at_scan_start"),
                    "stage1_remaining_ms_at_scan_start": scan_stats.get("stage1_remaining_ms_at_scan_start"),
                    "stage1_remaining_ms_before_scheduling": scan_stats.get("stage1_remaining_ms_before_scheduling"),
                    "scan_schedule_ms": scan_stats.get("scan_schedule_ms"),
                    "scan_await_ms": scan_stats.get("scan_await_ms"),
                    "expand_ms": scan_stats.get("expand_ms"),
                    "beam_ms": scan_stats.get("beam_ms"),
                    "candidates_initial": scan_stats.get("candidates_initial"),
                    "routes_for_expand": scan_stats.get("routes_for_expand"),
                    "routes_pruned_for_expand": scan_stats.get("routes_pruned_for_expand"),
                    "candidates_after_beam": scan_stats.get("candidates_after_beam"),
                    "candidates_after_multidex": scan_stats.get("candidates_after_multidex"),
                    "capped_by_max_candidates_stage1": scan_stats.get("capped_by_max_candidates_stage1"),
                    "capped_by_max_total_expanded": scan_stats.get("capped_by_max_total_expanded"),
                    "capped_by_max_expanded_per_candidate": scan_stats.get("capped_by_max_expanded_per_candidate"),
                    "viability_fallback_used": scan_stats.get("viability_fallback_used"),
                    "beam_fallback_used": scan_stats.get("beam_fallback_used"),
                    "reason_if_zero_scheduled": scan_stats.get("reason_if_zero_scheduled"),
                    "sanity_rejects_total": scan_stats.get("sanity_rejects_total"),
                    "rejects_by_reason": scan_stats.get("rejects_by_reason"),
                    "text": (
                        f"Scanned {int(finished_count)}/{int(candidates_count)} routes | "
                        f"raw={int(funnel_counts['raw'])} gas={int(funnel_counts['gas'])} "
                        f"safety={int(funnel_counts['safety'])} ready={int(funnel_counts['ready'])} | "
                        f"best={best_profit:.6f} | mode={s.scan_mode}"
                    ),
                }
            )

            # Emit RPC pool stats + block summary for debugging
            try:
                if hasattr(PS.rpc, "stats"):
                    await ui_push({
                        "type": "rpc_stats",
                        "time": datetime.utcnow().strftime("%H:%M:%S"),
                        "block": int(block_ctx.block_number),
                        "stats": PS.rpc.stats(),
                    })
            except Exception:
                pass
            try:
                BLOCK_LOG.open("a", encoding="utf-8").write(json.dumps({
                    "time": datetime.utcnow().isoformat(),
                    "block": int(block_ctx.block_number),
                    "candidates": int(candidates_count),
                    "candidates_total": int(candidates_count),
                    "finished": int(finished_count),
                    "scan_scheduled": int(scan_stats.get("scheduled", 0)),
                    "scan_invalid": int(scan_stats.get("invalid", 0)),
                    "scan_timeouts": int(scan_stats.get("timeouts", 0)),
                    "scan_budget_skipped": int(scan_stats.get("budget_skipped", 0)),
                    "prepare_ms": scan_stats.get("prepare_ms"),
                    "scan_start_delay_ms": scan_stats.get("scan_start_delay_ms"),
                    "stage1_deadline_remaining_ms_at_scan_start": scan_stats.get("stage1_deadline_remaining_ms_at_scan_start"),
                    "stage1_remaining_ms_at_scan_start": scan_stats.get("stage1_remaining_ms_at_scan_start"),
                    "stage1_remaining_ms_before_scheduling": scan_stats.get("stage1_remaining_ms_before_scheduling"),
                    "scan_schedule_ms": scan_stats.get("scan_schedule_ms"),
                    "scan_await_ms": scan_stats.get("scan_await_ms"),
                    "expand_ms": scan_stats.get("expand_ms"),
                    "beam_ms": scan_stats.get("beam_ms"),
                    "candidates_initial": scan_stats.get("candidates_initial"),
                    "routes_for_expand": scan_stats.get("routes_for_expand"),
                    "routes_pruned_for_expand": scan_stats.get("routes_pruned_for_expand"),
                    "candidates_after_beam": scan_stats.get("candidates_after_beam"),
                    "candidates_after_multidex": scan_stats.get("candidates_after_multidex"),
                    "capped_by_max_candidates_stage1": scan_stats.get("capped_by_max_candidates_stage1"),
                    "capped_by_max_total_expanded": scan_stats.get("capped_by_max_total_expanded"),
                    "capped_by_max_expanded_per_candidate": scan_stats.get("capped_by_max_expanded_per_candidate"),
                    "viability_fallback_used": scan_stats.get("viability_fallback_used"),
                    "beam_fallback_used": scan_stats.get("beam_fallback_used"),
                    "reason_if_zero_scheduled": scan_stats.get("reason_if_zero_scheduled"),
                    "sanity_rejects_total": scan_stats.get("sanity_rejects_total"),
                    "rejects_by_reason": scan_stats.get("rejects_by_reason"),
                    "profitable": int(len(profitable)),
                    "best_profit": float(best_profit),
                    "raw_opps": int(funnel_counts["raw"]),
                    "raw_gross_hits": int(funnel_counts["raw"]),
                    "safety_opps": int(funnel_counts["safety"]),
                    "safety_hits": int(funnel_counts["safety"]),
                    "gas_opps": int(funnel_counts["gas"]),
                    "net_hits": int(funnel_counts["gas"]),
                    "final_opps": int(funnel_counts["ready"]),
                    "ready_hits": int(funnel_counts["ready"]),
                    "top_examples": top_examples,
                    "profit_gross_min": funnel_stats.get("gross_min"),
                    "profit_gross_max": funnel_stats.get("gross_max"),
                    "profit_net_min": funnel_stats.get("net_min"),
                    "profit_net_max": funnel_stats.get("net_max"),
                    "gas_units_min": funnel_stats.get("gas_units_min"),
                    "gas_units_max": funnel_stats.get("gas_units_max"),
                    "gas_units_avg": funnel_stats.get("gas_units_avg"),
                    "gas_units_median": funnel_stats.get("gas_units_median"),
                    "gas_cost_min": funnel_stats.get("gas_cost_min"),
                    "gas_cost_max": funnel_stats.get("gas_cost_max"),
                    "gas_cost_avg": funnel_stats.get("gas_cost_avg"),
                    "gas_cost_median": funnel_stats.get("gas_cost_median"),
                    "mode": str(s.scan_mode),
                }, ensure_ascii=False) + "\n")
            except Exception:
                pass
            blocks_scanned += 1
            profit_hits += int(len(profitable))
            _write_session_summary(
                started_at=session_started_at,
                updated_at=datetime.utcnow(),
                duration_s=(datetime.utcnow() - session_started_at).total_seconds(),
                rpc_urls=rpc_urls,
                dexes=dexes_used,
                blocks_scanned=blocks_scanned,
                profit_hits=profit_hits,
                settings=s,
                env_flags={
                    "SIM_PROFILE": str(os.getenv("SIM_PROFILE", "")),
                    "DEBUG_FUNNEL": "1" if _env_flag("DEBUG_FUNNEL") else "0",
                    "GAS_OFF": "1" if _env_flag("GAS_OFF") else "0",
                    "FIXED_GAS_UNITS": str(os.getenv("FIXED_GAS_UNITS", "")),
                    "ENABLE_MULTIDEX": "1" if enable_multidex else "0",
                    "SCAN_SOURCE": str(scan_source),
                    "MEMPOOL_ENABLED": "1" if mempool_on else "0",
                },
            )

            for payload in profitable:
                await simulate(payload, int(block_ctx.block_number))
            log(iteration, profitable, int(block_ctx.block_number))
            await _confirm_mempool(mempool_engine, int(block_ctx.block_number))
            if stop_after_blocks > 0 and blocks_seen >= stop_after_blocks:
                break

    finally:
        if mempool_engine:
            await mempool_engine.stop()
        if snapshotter:
            snapshotter.write_snapshot(reason="shutdown")
            await snapshotter.stop()
        try:
            cfg_path = Path(os.getenv("BOT_CONFIG", "bot_config.json"))
            write_assistant_pack(log_dir=LOG_DIR, config_path=cfg_path)
        except Exception:
            pass
        _write_session_summary(
            started_at=session_started_at,
            updated_at=datetime.utcnow(),
            duration_s=(datetime.utcnow() - session_started_at).total_seconds(),
            rpc_urls=rpc_urls,
            dexes=dexes_used,
            blocks_scanned=blocks_scanned,
            profit_hits=profit_hits,
            settings=s0,
            env_flags={
                "SIM_PROFILE": str(os.getenv("SIM_PROFILE", "")),
                "DEBUG_FUNNEL": "1" if _env_flag("DEBUG_FUNNEL") else "0",
                "GAS_OFF": "1" if _env_flag("GAS_OFF") else "0",
                "FIXED_GAS_UNITS": str(os.getenv("FIXED_GAS_UNITS", "")),
                "ENABLE_MULTIDEX": "1" if enable_multidex else "0",
                "SCAN_SOURCE": str(scan_source),
                "MEMPOOL_ENABLED": "1" if mempool_on else "0",
            },
        )
        try:
            await PS.rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        if "--dump-diagnostic" in sys.argv:
            s0 = _load_settings()
            try:
                window_s = int(float(os.getenv("DIAGNOSTIC_SNAPSHOT_WINDOW_S", "") or 900))
            except Exception:
                window_s = 900
            snap = build_diagnostic_snapshot(
                log_dir=LOG_DIR,
                settings=s0,
                session_started_at_s=None,
                current_block=None,
                rpc_stats=None,
                window_s=window_s,
                update_reason="manual",
            )
            print(json.dumps(snap))
        elif "--preflight-replay" in sys.argv:
            limit = 20
            for idx, arg in enumerate(sys.argv):
                if arg.startswith("--preflight-replay="):
                    try:
                        limit = int(arg.split("=", 1)[1])
                    except Exception:
                        limit = 20
                if arg in ("--limit", "--replay-limit") and idx + 1 < len(sys.argv):
                    try:
                        limit = int(sys.argv[idx + 1])
                    except Exception:
                        limit = 20
            asyncio.run(replay_preflight(limit))
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Fork simulation stopped by user")
