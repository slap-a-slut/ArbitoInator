# sim/fork_test.py

import asyncio
import json
import os
import math
import statistics
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bot import scanner, strategies, executor, config
from bot.routes import Hop
from infra.rpc import get_provider
from ui_notify import ui_push


# w3 + scanner are initialized in main() after reading UI config
w3 = None  # type: ignore[assignment]
PS = None  # type: ignore[assignment]

SIM_FROM_ADDRESS = "0x0000000000000000000000000000000000000000"


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


LOG_DIR = Path("logs")
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
    dexes: Tuple[str, ...] = ()
    enable_multidex: bool = False
    max_hops: int = 3
    beam_k: int = 20
    edge_top_m: int = 2
    probe_amount: float = 1.0

    # Mode
    scan_mode: str = "auto"  # auto|fixed

    # Thresholds
    min_profit_pct: float = 0.05  # percent of input (e.g. 0.05 == 0.05%)
    min_profit_abs: float = 0.05  # in base token units (usually USD if base is USDC/USDT)
    slippage_bps: float = 8.0
    mev_buffer_bps: float = 5.0
    max_gas_gwei: Optional[float] = None

    # Performance
    concurrency: int = 12
    block_budget_s: float = 10.0

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

    # Reporting / base currency (UI should stay consistent)
    # Profits, spent, ROI are computed and displayed in this token.
    report_currency: str = "USDC"  # USDC|USDT


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
    # normalize tuples
    try:
        ru = raw.get("rpc_urls")
        if isinstance(ru, (list, tuple)):
            s.rpc_urls = tuple(str(x).strip() for x in ru if str(x).strip())
        elif isinstance(ru, str):
            s.rpc_urls = tuple(x.strip() for x in ru.split(",") if x.strip())
    except Exception:
        pass
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
        emd = raw.get("enable_multidex", s.enable_multidex)
        if isinstance(emd, str):
            s.enable_multidex = emd.strip().lower() in ("1", "true", "yes", "on")
        else:
            s.enable_multidex = bool(emd)
    except Exception:
        s.enable_multidex = False
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


def _scale_amount(token_in: str, amount_units: float) -> int:
    dec = _decimals_by_token(token_in)
    return int(float(amount_units) * (10 ** dec))


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


async def wait_for_new_block(last_block: int) -> int:
    while True:
        try:
            current_block = w3.eth.block_number
        except Exception:
            await asyncio.sleep(0.25)
            continue
        if current_block > last_block:
            return int(current_block)
        await asyncio.sleep(0.25)


async def scan_routes() -> List[Tuple[str, ...]]:
    s = _load_settings()
    rc = str(getattr(s, "report_currency", "USDC") or "USDC").upper()
    # Ensure we always scan cycles that start/end in the reporting currency,
    # so profit/spent symbols in the UI cannot drift.
    base_addr = config.TOKENS.get(rc, config.TOKENS["USDC"])
    return strategies.Strategy(bases=[base_addr]).get_routes(max_hops=int(getattr(s, "max_hops", 3)))


async def _route_viable(
    route: Tuple[str, ...],
    *,
    block: str,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    probe_units: float,
) -> bool:
    amt = _scale_amount(route[0], float(probe_units))
    for i in range(len(route) - 1):
        edge = await PS.best_edge(
            route[i],
            route[i + 1],
            amt,
            block=block,
            fee_tiers=fee_tiers,
            timeout_s=timeout_s,
        )
        if not edge or int(edge.amount_out) <= 0:
            return False
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
    block: str,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    beam_k: int,
    edge_top_m: int,
    eval_budget: int,
) -> List[Dict[str, Any]]:
    states: List[Dict[str, Any]] = [{"amount": int(amount_in), "hops": []}]
    max_drawdown = float(getattr(config, "BEAM_MAX_DRAWDOWN", 0.35))
    max_drawdown = min(0.9, max(0.0, max_drawdown))

    for i in range(len(route) - 1):
        token_in = route[i]
        token_out = route[i + 1]
        new_states: List[Dict[str, Any]] = []
        for st in states:
            edges = await PS.quote_edges(
                token_in,
                token_out,
                int(st["amount"]),
                block=block,
                fee_tiers=fee_tiers,
                timeout_s=timeout_s,
            )
            if not edges:
                continue
            edges = sorted(edges, key=lambda e: int(e.amount_out), reverse=True)
            edges = edges[: max(1, int(edge_top_m))]
            for edge in edges:
                if len(new_states) >= int(eval_budget):
                    break
                if int(edge.amount_out) <= 0:
                    continue
                next_hops = list(st["hops"]) + [_edge_to_hop(edge, token_in, token_out)]
                new_states.append({"amount": int(edge.amount_out), "hops": next_hops})
            if len(new_states) >= int(eval_budget):
                break

        if not new_states:
            return []

        new_states = sorted(new_states, key=lambda st: int(st["amount"]), reverse=True)
        if max_drawdown > 0:
            floor_amt = int(float(amount_in) * (1.0 - max_drawdown))
            new_states = [st for st in new_states if int(st["amount"]) >= floor_amt]
        states = new_states[: max(1, int(beam_k))]

        if not states:
            return []

    out: List[Dict[str, Any]] = []
    for st in states:
        out.append({"route": route, "hops": list(st["hops"]), "amount_in": int(amount_in)})
    return out


async def _expand_multidex_candidates(
    routes: List[Tuple[str, ...]],
    *,
    amount_in: int,
    block_number: int,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    deadline_s: float,
    settings: Settings,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(max(1, int(settings.concurrency)))
    block_tag = hex(int(block_number))
    results: List[Dict[str, Any]] = []

    async def one(route: Tuple[str, ...]) -> List[Dict[str, Any]]:
        async with sem:
            if loop.time() >= deadline_s:
                return []
            try:
                ok = await _route_viable(
                    route,
                    block=block_tag,
                    fee_tiers=fee_tiers,
                    timeout_s=timeout_s,
                    probe_units=float(settings.probe_amount),
                )
                if not ok:
                    return []
                return await _beam_candidates_for_route(
                    route,
                    amount_in=int(amount_in),
                    block=block_tag,
                    fee_tiers=fee_tiers,
                    timeout_s=timeout_s,
                    beam_k=int(settings.beam_k),
                    edge_top_m=int(settings.edge_top_m),
                    eval_budget=int(settings.stage2_max_evals),
                )
            except Exception:
                return []

    tasks: List[asyncio.Task] = []
    for route in routes:
        if loop.time() >= deadline_s:
            break
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
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return results


async def _scan_candidates(
    candidates: List[Dict[str, Any]],
    block_number: int,
    *,
    fee_tiers: Optional[List[int]],
    timeout_s: float,
    deadline_s: float,
    keep_all: bool,
) -> Tuple[List[Dict[str, Any]], int]:
    """Scan candidates until deadline.

    Returns (payloads, finished_count)
    """

    s = _load_settings()
    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(max(1, int(s.concurrency)))
    block_tag = hex(int(block_number))

    results: List[Dict[str, Any]] = []
    finished = 0

    async def one(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        nonlocal finished
        async with sem:
            try:
                # Enforce per-candidate timeout hard
                payload = await asyncio.wait_for(
                    PS.price_payload(c, block=block_tag, fee_tiers=fee_tiers, timeout_s=timeout_s),
                    timeout=timeout_s + 0.5,
                )
            except Exception:
                payload = None
            finished += 1
            return payload

    tasks: List[asyncio.Task] = []
    for c in candidates:
        if loop.time() >= deadline_s:
            break
        tasks.append(asyncio.create_task(one(c)))

    try:
        # Avoid asyncio.as_completed(...)+break warnings ("_wait_for_one was never awaited").
        pending = set(tasks)
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
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return results, finished


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


def _debug_funnel_log(payloads: List[Dict[str, Any]], s: Settings, block_number: int) -> None:
    if not _env_flag("DEBUG_FUNNEL"):
        return
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
    block_number: int,
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
                PS.price_payload(c, block=hex(int(block_number)), fee_tiers=None, timeout_s=float(s.rpc_timeout_stage2_s)),
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
    seed_amt = int(seed_payload.get("amount_in", _scale_amount(token_in, float(s.stage1_amount))))
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
    tx = executor.prepare_transaction(payload, SIM_FROM_ADDRESS)
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


async def main() -> None:
    iteration = 0
    print("🚀 Fork simulation started...")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": None, "text": "started"})

    # Init RPC endpoints (multi-RPC failover supported)
    global w3, PS
    s0 = _load_settings()
    rpc_urls = list(s0.rpc_urls) if getattr(s0, "rpc_urls", None) else []
    if not rpc_urls:
        # env RPC_URLS / config fallback
        raw = os.getenv("RPC_URLS")
        if raw:
            rpc_urls = [x.strip() for x in raw.split(",") if x.strip()]
    if not rpc_urls:
        rpc_urls = [os.getenv("RPC_URL", config.RPC_URL)]

    w3 = get_provider(rpc_urls)
    enable_multidex = bool(getattr(s0, "enable_multidex", False)) or _env_flag("ENABLE_MULTIDEX")
    dexes = list(s0.dexes) if getattr(s0, "dexes", None) else None
    if not enable_multidex:
        dexes = ["univ3"]
    PS = scanner.PriceScanner(rpc_urls=rpc_urls, dexes=dexes)
    dexes_used = list(getattr(PS, "dexes", []) or (dexes or []))

    session_started_at = datetime.utcnow()
    blocks_scanned = 0
    profit_hits = 0

    last_block = w3.eth.block_number
    print(f"[RPC] connected={w3.is_connected()} | block={last_block} | urls={len(rpc_urls)}")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": last_block, "text": f"rpc connected={w3.is_connected()} | urls={len(rpc_urls)}"})

    try:
        while True:
            iteration += 1
            s = _load_settings()
            try:
                config.V2_MIN_RESERVE_RATIO = float(s.v2_min_reserve_ratio)
                config.V2_MAX_PRICE_IMPACT_BPS = float(s.v2_max_price_impact_bps)
            except Exception:
                pass

            # 0) wait new block
            block_number = await wait_for_new_block(last_block)
            last_block = block_number
            print(f"Block {block_number}")

            # Optional gas gate
            if s.max_gas_gwei is not None:
                try:
                    gas_gwei = float(w3.eth.gas_price) / 1e9
                    if gas_gwei > float(s.max_gas_gwei):
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
                        continue
                except Exception:
                    pass

            await ui_push({"type": "scan", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": "Scanning routes..."})

            loop = asyncio.get_running_loop()
            start_t = loop.time()
            deadline = start_t + float(s.block_budget_s)
            stage1_deadline = start_t + float(s.block_budget_s) * 0.60

            # prepare per-block caches (gas + WETH/USDC warmup)
            try:
                await PS.prepare_block(int(block_number))
            except Exception:
                pass

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
                    },
                )
                continue

            profitable: List[Dict[str, Any]] = []
            candidates_count = 0
            finished_count = 0
            funnel_payloads: List[Dict[str, Any]] = []
            funnel_counts = {"raw": 0, "gas": 0, "safety": 0, "ready": 0}
            funnel_stats: Dict[str, Optional[float]] = {}
            top_examples: Dict[str, List[Dict[str, Any]]] = {}

            try:
                if str(s.scan_mode).lower() == "fixed":
                    candidates: List[Dict[str, Any]] = []
                    if enable_multidex:
                        base_token = routes[0][0]
                        for u in list(s.amount_presets):
                            amt_in = _scale_amount(base_token, float(u))
                            more = await _expand_multidex_candidates(
                                routes,
                                amount_in=int(amt_in),
                                block_number=block_number,
                                fee_tiers=None,
                                timeout_s=float(s.rpc_timeout_stage2_s),
                                deadline_s=deadline,
                                settings=s,
                            )
                            if more:
                                candidates.extend(more)
                    else:
                        for route in routes:
                            token_in = route[0]
                            for u in list(s.amount_presets):
                                candidates.append({"route": route, "amount_in": _scale_amount(token_in, float(u))})
                    candidates_count = len(candidates)
                    payloads, finished_count = await _scan_candidates(
                        candidates,
                        block_number,
                        fee_tiers=None,
                        timeout_s=float(s.rpc_timeout_stage2_s),
                        deadline_s=deadline,
                        keep_all=True,
                    )
                    payloads = [p for p in payloads if p and p.get("profit_raw", -1) != -1]
                    payloads2 = [_apply_safety(p, s) for p in payloads]
                    funnel_payloads = payloads2
                    funnel_counts = _funnel_counts(funnel_payloads, s)
                    funnel_stats = _funnel_stats(funnel_payloads)
                    top_examples = _top_examples(funnel_payloads)
                    _debug_funnel_log(funnel_payloads, s, block_number)
                    for p2 in payloads2:
                        if strategies.risk_check(p2) and _passes_thresholds(p2, s):
                            profitable.append(p2)

                else:
                    # Stage 1
                    if enable_multidex:
                        base_token = routes[0][0]
                        stage1_amount = _scale_amount(base_token, float(s.stage1_amount))
                        stage1_candidates = await _expand_multidex_candidates(
                            routes,
                            amount_in=int(stage1_amount),
                            block_number=block_number,
                            fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
                            timeout_s=float(s.rpc_timeout_stage1_s),
                            deadline_s=stage1_deadline,
                            settings=s,
                        )
                    else:
                        stage1_candidates = [
                            {"route": route, "amount_in": _scale_amount(route[0], float(s.stage1_amount))} for route in routes
                        ]
                    candidates_count = len(stage1_candidates)

                    stage1_payloads, finished_count = await _scan_candidates(
                        stage1_candidates,
                        block_number,
                        fee_tiers=list(s.stage1_fee_tiers) if s.stage1_fee_tiers else None,
                        timeout_s=float(s.rpc_timeout_stage1_s),
                        deadline_s=stage1_deadline,
                        keep_all=True,
                    )

                    stage1_payloads = [p for p in stage1_payloads if p and p.get("profit_raw", -1) != -1]
                    stage1_payloads2 = [_apply_safety(p, s) for p in stage1_payloads]
                    funnel_payloads = stage1_payloads2
                    funnel_counts = _funnel_counts(funnel_payloads, s)
                    funnel_stats = _funnel_stats(funnel_payloads)
                    top_examples = _top_examples(funnel_payloads)
                    _debug_funnel_log(funnel_payloads, s, block_number)
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

                    topk = soft[: int(s.stage2_top_k)]
                    sem2 = asyncio.Semaphore(max(1, min(int(s.concurrency), 12)))

                    async def opt_one(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                        async with sem2:
                            return await _optimize_amount_for_route(tuple(p.get("route")), block_number, p, deadline_s=deadline)

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
                        if strategies.risk_check(bp2) and _passes_thresholds(bp2, s):
                            profitable.append(bp2)

            except Exception as e:
                print(f"[WARN] scan failed: {type(e).__name__}: {e}")
                await ui_push({"type": "warn", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": f"scan failed: {type(e).__name__}"})
                await asyncio.sleep(0.5)
                continue

            if not profitable:
                await ui_push(
                    {
                        "type": "scan",
                        "time": datetime.utcnow().strftime("%H:%M:%S"),
                        "block": block_number,
                        "candidates": int(candidates_count),
                        "profitable": 0,
                        "best_profit": 0.0,
                        "raw_opps": int(funnel_counts["raw"]),
                        "safety_opps": int(funnel_counts["safety"]),
                        "gas_opps": int(funnel_counts["gas"]),
                        "final_opps": int(funnel_counts["ready"]),
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
                            "block": block_number,
                            "stats": PS.rpc.stats(),
                        })
                except Exception:
                    pass
                try:
                    BLOCK_LOG.open("a", encoding="utf-8").write(json.dumps({
                        "time": datetime.utcnow().isoformat(),
                        "block": int(block_number),
                        "candidates": int(candidates_count),
                        "candidates_total": int(candidates_count),
                        "finished": int(finished_count),
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
                    },
                )
                continue

            best_profit = max((float(p.get("profit_adj", p.get("profit", 0))) for p in profitable), default=0.0)
            await ui_push(
                {
                    "type": "scan",
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "block": block_number,
                    "candidates": int(candidates_count),
                    "profitable": len(profitable),
                    "best_profit": float(best_profit),
                    "raw_opps": int(funnel_counts["raw"]),
                    "safety_opps": int(funnel_counts["safety"]),
                    "gas_opps": int(funnel_counts["gas"]),
                    "final_opps": int(funnel_counts["ready"]),
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
                        "block": block_number,
                        "stats": PS.rpc.stats(),
                    })
            except Exception:
                pass
            try:
                BLOCK_LOG.open("a", encoding="utf-8").write(json.dumps({
                    "time": datetime.utcnow().isoformat(),
                    "block": int(block_number),
                    "candidates": int(candidates_count),
                    "candidates_total": int(candidates_count),
                    "finished": int(finished_count),
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
                },
            )

            for payload in profitable:
                await simulate(payload, block_number)
            log(iteration, profitable, block_number)

    finally:
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
            },
        )
        try:
            await PS.rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Fork simulation stopped by user")
