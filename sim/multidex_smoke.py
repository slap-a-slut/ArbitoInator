import asyncio
import os
from typing import Any, Dict, List, Tuple

from bot import config, scanner, strategies
from bot.routes import Hop
from infra.rpc import get_provider, get_rpc_urls


def _scale_amount(token_in: str, amount_units: float) -> int:
    dec = int(config.token_decimals(token_in))
    return int(float(amount_units) * (10 ** dec))


def _route_str(route: Tuple[str, ...], dex_path: Tuple[str, ...]) -> str:
    parts: List[str] = []
    for i, addr in enumerate(route):
        parts.append(config.token_symbol(addr))
        if i < len(dex_path):
            parts.append(f"-[{dex_path[i]}]->")
    return " ".join(parts)


def _edge_to_hop(edge: Any, token_in: str, token_out: str) -> Hop:
    params: Dict[str, Any] = {}
    try:
        tier = edge.meta.get("fee_tier") if getattr(edge, "meta", None) else None
        if tier is not None:
            params["fee_tier"] = int(tier)
    except Exception:
        pass
    return Hop(token_in=token_in, token_out=token_out, dex_id=str(edge.dex_id), params=params)


async def _beam_route(
    ps: scanner.PriceScanner,
    route: Tuple[str, ...],
    *,
    amount_in: int,
    block: str,
    fee_tiers: List[int],
    beam_k: int,
    edge_top_m: int,
) -> List[Dict[str, Any]]:
    states: List[Dict[str, Any]] = [{"amount": int(amount_in), "hops": []}]
    for i in range(len(route) - 1):
        token_in = route[i]
        token_out = route[i + 1]
        new_states: List[Dict[str, Any]] = []
        for st in states:
            edges = await ps.quote_edges(
                token_in,
                token_out,
                int(st["amount"]),
                block=block,
                fee_tiers=fee_tiers,
                timeout_s=4.0,
            )
            if not edges:
                continue
            edges = sorted(edges, key=lambda e: int(e.amount_out), reverse=True)[: max(1, edge_top_m)]
            for edge in edges:
                if int(edge.amount_out) <= 0:
                    continue
                next_hops = list(st["hops"]) + [_edge_to_hop(edge, token_in, token_out)]
                new_states.append({"amount": int(edge.amount_out), "hops": next_hops})
        if not new_states:
            return []
        new_states = sorted(new_states, key=lambda st: int(st["amount"]), reverse=True)
        states = new_states[: max(1, beam_k)]
    return states


async def main() -> None:
    rpc_urls = get_rpc_urls()
    w3 = get_provider(rpc_urls)
    block_number = int(os.getenv("BLOCK_NUMBER") or w3.eth.block_number)
    block_tag = hex(block_number)

    dexes = os.getenv("DEXES", "univ3,univ2,sushiswap").split(",")
    dexes = [d.strip().lower() for d in dexes if d.strip()]
    ps = scanner.PriceScanner(rpc_urls=rpc_urls, dexes=dexes)
    await ps.prepare_block(block_number)

    base = config.TOKENS.get("USDC", config.TOKENS["USDC"])
    routes = strategies.Strategy(bases=[base]).get_routes(max_hops=3)
    routes = routes[:20]

    amount_in = _scale_amount(base, float(os.getenv("AMOUNT_IN", "1000")))
    fee_tiers = [int(x) for x in os.getenv("FEE_TIERS", "500,3000").split(",") if x.strip()]
    beam_k = int(os.getenv("BEAM_K", "10"))
    edge_top_m = int(os.getenv("EDGE_TOP_M", "2"))

    candidates: List[Dict[str, Any]] = []
    for route in routes:
        states = await _beam_route(
            ps,
            route,
            amount_in=amount_in,
            block=block_tag,
            fee_tiers=fee_tiers,
            beam_k=beam_k,
            edge_top_m=edge_top_m,
        )
        for st in states:
            candidates.append({"route": route, "hops": list(st["hops"]), "amount_in": int(amount_in)})

    payloads: List[Dict[str, Any]] = []
    for c in candidates:
        p = await ps.price_payload(c, block=block_tag, fee_tiers=fee_tiers, timeout_s=4.0)
        if p and p.get("profit_raw", -1) != -1:
            payloads.append(p)

    gross_sorted = sorted(payloads, key=lambda p: int(p.get("profit_gross_raw", 0)), reverse=True)
    net_sorted = sorted(payloads, key=lambda p: int(p.get("profit_raw_net", p.get("profit_raw", 0) or 0)), reverse=True)

    print(f"Block {block_number} | routes={len(routes)} | candidates={len(candidates)} | payloads={len(payloads)}")
    print("Top gross:")
    for p in gross_sorted[:5]:
        route = _route_str(tuple(p.get("route") or ()), tuple(p.get("dex_path") or ()))
        print(f"  gross={p.get('profit_gross', 0):.6f} net={p.get('profit_net', p.get('profit', 0)):.6f} | {route}")
    print("Top net:")
    for p in net_sorted[:5]:
        route = _route_str(tuple(p.get("route") or ()), tuple(p.get("dex_path") or ()))
        print(f"  net={p.get('profit_net', p.get('profit', 0)):.6f} gross={p.get('profit_gross', 0):.6f} | {route}")


if __name__ == "__main__":
    asyncio.run(main())
