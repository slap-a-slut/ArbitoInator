# sim/fork_test.py

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bot import scanner, strategies, executor, config
from infra.rpc import get_provider
from ui_notify import ui_push


w3 = get_provider()  # sync provider for block_number only
PS = scanner.PriceScanner(os.getenv("RPC_URL", config.RPC_URL))

SIM_FROM_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass
class Settings:
    # Mode
    scan_mode: str = "auto"  # auto|fixed

    # Thresholds
    min_profit_pct: float = 0.05  # percent of input (e.g. 0.05 == 0.05%)
    min_profit_abs: float = 0.05  # in base token units (usually USD if base is USDC/USDT)
    slippage_bps: float = 8.0
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
        s.amount_presets = tuple(float(x) for x in (raw.get("amount_presets") or s.amount_presets))
    except Exception:
        pass
    try:
        s.stage1_fee_tiers = tuple(int(x) for x in (raw.get("stage1_fee_tiers") or s.stage1_fee_tiers))
    except Exception:
        pass
    return s


def _decimals_by_token(addr: str) -> int:
    a = str(addr).lower()
    for sym, v in config.TOKENS.items():
        if str(v).lower() == a:
            return int(config.TOKEN_DECIMALS.get(sym, 18))
    return 18


def _scale_amount(token_in: str, amount_units: float) -> int:
    dec = _decimals_by_token(token_in)
    return int(float(amount_units) * (10 ** dec))


def _route_pretty(route: Tuple[str, ...]) -> str:
    out = []
    for addr in route:
        sym = None
        a = str(addr).lower()
        for s, v in config.TOKENS.items():
            if str(v).lower() == a:
                sym = s
                break
        out.append(sym or (str(addr)[:6] + "..." + str(addr)[-4:]))
    return "→".join(out)


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
    return strategies.Strategy().get_routes()


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
        for fut in asyncio.as_completed(tasks):
            if loop.time() >= deadline_s:
                break
            p = await fut
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
        amt_in = int(p.get("amount_in", 0))
        dec = _decimals_by_token(p.get("route")[0])
        safety_raw = int(amt_in * float(s.slippage_bps) / 10_000.0)
        p2 = dict(p)
        p2["profit_raw_adj"] = int(p.get("profit_raw", 0)) - safety_raw
        p2["profit_adj"] = float(p2["profit_raw_adj"]) / float(10 ** dec)
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
    await ui_push(
        {
            "type": "profit",
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "block": block_number,
            "text": f"profit={payload.get('profit',0):.6f} {payload.get('token_in_symbol','')} route={_route_pretty(tuple(payload.get('route')))} gas={payload.get('gas_units','?')}",
        }
    )
    print(
        f"[Executor] Simulating tx from {tx.get('from')} to {tx.get('to')} | Profit: {payload.get('profit',0):.6f}"
    )
    await asyncio.sleep(0)


def log(iteration: int, payloads: List[Dict[str, Any]], block_number: int) -> None:
    now = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{now}] Block {block_number} | Iteration {iteration} | Profitable payloads: {len(payloads)}")
    for p in payloads:
        print(f"  Route: {_route_pretty(tuple(p.get('route')))} | Profit: {p.get('profit',0):.6f}")


async def main() -> None:
    iteration = 0
    print("🚀 Fork simulation started...")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": None, "text": "started"})

    last_block = w3.eth.block_number
    print(f"[RPC] connected={w3.is_connected()} | block={last_block}")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": last_block, "text": f"rpc connected={w3.is_connected()}"})

    try:
        while True:
            iteration += 1
            s = _load_settings()

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
                continue

            profitable: List[Dict[str, Any]] = []
            candidates_count = 0
            finished_count = 0

            try:
                if str(s.scan_mode).lower() == "fixed":
                    candidates: List[Dict[str, Any]] = []
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
                        keep_all=False,
                    )
                    for p in payloads:
                        p2 = _apply_safety(p, s)
                        if strategies.risk_check(p2) and _passes_thresholds(p2, s):
                            profitable.append(p2)

                else:
                    # Stage 1
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
                        for fut in asyncio.as_completed(opt_tasks):
                            if loop.time() >= deadline:
                                break
                            bp = await fut
                            if bp:
                                best_payloads.append(bp)
                    finally:
                        for t in opt_tasks:
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(*opt_tasks, return_exceptions=True)

                    for bp in best_payloads:
                        bp2 = _apply_safety(bp, s)
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
                        "text": f"Scanned {int(finished_count)}/{int(candidates_count)} routes | profitable=0 | mode={s.scan_mode}",
                    }
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
                    "text": f"Scanned {int(finished_count)}/{int(candidates_count)} routes | profitable={len(profitable)} | best={best_profit:.6f} | mode={s.scan_mode}",
                }
            )

            for payload in profitable:
                await simulate(payload, block_number)
            log(iteration, profitable, block_number)

    finally:
        try:
            await PS.rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Fork simulation stopped by user")


async def simulate(payload: Dict[str, Any], block_number: int):
    tx = executor.prepare_transaction(payload, SIM_FROM_ADDRESS)
    await ui_push(
        {
            "type": "profit",
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "block": block_number,
            "text": f"profit={payload.get('profit',0):.6f} route={_route_pretty(tuple(payload.get('route',()))) }",
        }
    )
    _ = tx
    await asyncio.sleep(0)


def log(iteration: int, payloads: List[Dict[str, Any]], block_number: int):
    now = datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{now}] Block {block_number} | Iteration {iteration} | Profitable payloads: {len(payloads)}")
    for p in payloads:
        print(f"  Route: {_route_pretty(tuple(p['route']))} | Profit: {p.get('profit_adj',p.get('profit',0)):.6f}")


async def main():
    iteration = 0
    print("🚀 Fork simulation started...")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": None, "text": "started"})

    last_block = w3.eth.block_number
    print(f"[RPC] connected={w3.is_connected()} | block={last_block}")
    await ui_push({"type": "status", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": last_block, "text": f"rpc connected={w3.is_connected()}"})

    try:
        while True:
            iteration += 1
            s = _load_settings()

            block_number = await wait_for_new_block(int(last_block))
            last_block = block_number

            # Optional gas gate
            if s.max_gas_gwei is not None:
                try:
                    gas_gwei = float(w3.eth.gas_price) / 1e9
                    if gas_gwei > float(s.max_gas_gwei):
                        await ui_push({
                            "type": "scan",
                            "time": datetime.utcnow().strftime("%H:%M:%S"),
                            "block": block_number,
                            "candidates": 0,
                            "profitable": 0,
                            "best_profit": 0.0,
                            "text": f"Skipped: gas {gas_gwei:.1f} gwei > max {float(s.max_gas_gwei):.1f} gwei",
                        })
                        continue
                except Exception:
                    pass

            await ui_push({"type": "scan", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": "Scanning routes..."})

            loop = asyncio.get_running_loop()
            start_t = loop.time()
            deadline = start_t + float(s.block_budget_s)
            stage1_deadline = start_t + float(s.block_budget_s) * 0.60

            # Prepare block caches
            try:
                await PS.prepare_block(int(block_number))
            except Exception:
                pass

            routes = await scan_routes()
            if not routes:
                await ui_push({"type": "scan", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "candidates": 0, "profitable": 0, "best_profit": 0.0, "text": "No routes generated"})
                continue

            profitable: List[Dict[str, Any]] = []
            candidates_count = 0
            finished_count = 0

            try:
                if str(s.scan_mode).lower() == "fixed":
                    candidates: List[Dict[str, Any]] = []
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
                        keep_all=False,
                    )
                    for p in payloads:
                        p2 = _apply_safety(p, s)
                        if strategies.risk_check(p2) and _passes_thresholds(p2, s):
                            profitable.append(p2)
                else:
                    # Auto: Stage1 cheap scan
                    stage1_candidates = [{"route": r, "amount_in": _scale_amount(r[0], float(s.stage1_amount))} for r in routes]
                    candidates_count = len(stage1_candidates)

                    stage1_payloads, finished_count = await _scan_candidates(
                        stage1_candidates,
                        block_number,
                        fee_tiers=list(s.stage1_fee_tiers),
                        timeout_s=float(s.rpc_timeout_stage1_s),
                        deadline_s=stage1_deadline,
                        keep_all=True,
                    )

                    stage1_map = {tuple(p.get("route") or ()): p for p in stage1_payloads if p and p.get("profit_raw", -1) != -1}
                    ranked = sorted(stage1_map.values(), key=lambda p: int(p.get("profit_raw", -10**30)), reverse=True)
                    topk = ranked[: int(s.stage2_top_k)]

                    # Stage2 optimize amount only for top-K
                    sem2 = asyncio.Semaphore(max(1, min(int(s.concurrency), 12)))

                    async def opt_one(p):
                        async with sem2:
                            return await _optimize_amount_for_route(tuple(p["route"]), block_number, p, deadline_s=deadline)

                    opt_tasks = [asyncio.create_task(opt_one(p)) for p in topk]
                    best_payloads: List[Dict[str, Any]] = []
                    try:
                        for fut in asyncio.as_completed(opt_tasks):
                            if loop.time() >= deadline:
                                break
                            bp = await fut
                            if bp:
                                best_payloads.append(bp)
                    finally:
                        for t in opt_tasks:
                            if not t.done():
                                t.cancel()
                        await asyncio.gather(*opt_tasks, return_exceptions=True)

                    for bp in best_payloads:
                        bp2 = _apply_safety(bp, s)
                        if strategies.risk_check(bp2) and _passes_thresholds(bp2, s):
                            profitable.append(bp2)

            except Exception as e:
                print(f"[WARN] scan failed: {type(e).__name__}: {e}")
                await ui_push({"type": "warn", "time": datetime.utcnow().strftime("%H:%M:%S"), "block": block_number, "text": f"scan failed: {type(e).__name__}"})
                await asyncio.sleep(0.5)
                continue

            if not profitable:
                await ui_push({
                    "type": "scan",
                    "time": datetime.utcnow().strftime("%H:%M:%S"),
                    "block": block_number,
                    "candidates": int(candidates_count),
                    "profitable": 0,
                    "best_profit": 0.0,
                    "text": f"Scanned {int(finished_count)}/{int(candidates_count)} routes | profitable=0 | mode={s.scan_mode}",
                })
                continue

            best_profit = max((float(p.get("profit_adj", p.get("profit", 0.0))) for p in profitable), default=0.0)
            await ui_push({
                "type": "scan",
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "block": block_number,
                "candidates": int(candidates_count),
                "profitable": len(profitable),
                "best_profit": float(best_profit),
                "text": f"Scanned {int(finished_count)}/{int(candidates_count)} routes | profitable={len(profitable)} | best={best_profit:.6f} | mode={s.scan_mode}",
            })

            for payload in profitable:
                await simulate(payload, block_number)

            log(iteration, profitable, block_number)

    finally:
        try:
            await PS.rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Fork simulation stopped by user")
