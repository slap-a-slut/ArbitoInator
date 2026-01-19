# bot/scanner.py

"""High-performance scanner for multi-DEX quoting.

This version is designed specifically to:
 - never stall a block forever (timeouts + graceful degradation)
 - minimize RPC usage (per-block caches + gas warmup once per block)
 - support N-hop routes (2-hop, 3-hop, etc.)
"""

from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from bot import config
from bot.block_context import BlockContext
from bot.dex.base import QuoteEdge
from bot.dex.registry import build_adapters
from bot.routes import Hop
from infra.rpc import AsyncRPC, RPCPool, get_rpc_urls


def _sym_by_addr(addr: str) -> str:
    return config.token_symbol(addr)


def _decimals(token: str) -> int:
    # Hardcoded decimals for speed (no chain calls)
    return int(config.token_decimals(token))


def _param_key(params: Optional[Dict[str, Any]]) -> Tuple[Tuple[str, Any], ...]:
    if not params:
        return ()
    items: List[Tuple[str, Any]] = []
    for k in sorted(params.keys()):
        v = params[k]
        if isinstance(v, list):
            v = tuple(v)
        elif isinstance(v, dict):
            v = tuple(sorted((str(kk), vv) for kk, vv in v.items()))
        items.append((str(k), v))
    return tuple(items)


def _normalize_hops(hops: Optional[List[Any]]) -> Optional[List[Hop]]:
    if not hops:
        return None
    out: List[Hop] = []
    for h in hops:
        if isinstance(h, Hop):
            out.append(h)
            continue
        if isinstance(h, dict):
            try:
                out.append(
                    Hop(
                        token_in=config.token_address(str(h.get("token_in"))),
                        token_out=config.token_address(str(h.get("token_out"))),
                        dex_id=str(h.get("dex_id")),
                        params=dict(h.get("params") or {}),
                    )
                )
            except Exception:
                continue
    return out


def _hops_key(hops: Optional[List[Hop]]) -> Tuple[Tuple[str, Any], ...]:
    if not hops:
        return ()
    items: List[Tuple[str, Any]] = []
    for h in hops:
        items.append((h.dex_id, h.token_in, h.token_out, _param_key(h.params)))
    return tuple(items)


def _require_block_tag(block: Optional[str], block_ctx: Optional[BlockContext]) -> str:
    if block_ctx is not None:
        return str(block_ctx.block_tag)
    if block:
        return str(block)
    raise ValueError("block tag is required for scanning (no default 'latest')")


def _edge_fee_bps(edge: QuoteEdge) -> int:
    try:
        if edge.meta and edge.meta.get("fee_bps") is not None:
            return int(edge.meta.get("fee_bps"))
        if edge.meta and edge.meta.get("fee_tier") is not None:
            return int(edge.meta.get("fee_tier")) // 100
    except Exception:
        pass
    return 0


def _edge_fee_tier(edge: QuoteEdge) -> int:
    try:
        if edge.meta and edge.meta.get("fee_tier") is not None:
            return int(edge.meta.get("fee_tier"))
    except Exception:
        pass
    return 0


def _edge_label(edge: QuoteEdge) -> str:
    tier = _edge_fee_tier(edge)
    if tier > 0:
        return f"{edge.dex_id}:{tier}"
    return str(edge.dex_id)


def _edge_meta_snapshot(edge: QuoteEdge) -> Dict[str, Any]:
    meta = dict(edge.meta or {})
    return {
        "dex_id": str(edge.dex_id),
        "fee_bps": int(_edge_fee_bps(edge)) if _edge_fee_bps(edge) else None,
        "fee_tier": int(_edge_fee_tier(edge)) if _edge_fee_tier(edge) else None,
        "adapter": meta.get("adapter"),
        "raw_len": meta.get("raw_len"),
        "raw_prefix": meta.get("raw_prefix"),
        "rpc_url": meta.get("rpc_url"),
    }


def _is_nonsensical_amount(amount_in: int, amount_out: int) -> bool:
    # Guard against absurd quotes that usually come from decode/revert issues.
    try:
        if int(amount_in) <= 0 or int(amount_out) <= 0:
            return True
        if int(amount_out) > 10**36:
            return True
        if int(amount_out) > int(amount_in) * 10**12:
            return True
    except Exception:
        return True
    return False


def _classify_quote_error(err: Exception) -> str:
    msg = str(err).lower()
    if "revert" in msg:
        return "reverted"
    if "decode" in msg or "short blob" in msg:
        return "decode_error"
    if "timeout" in msg or "http_" in msg or "rpc call failed" in msg:
        return "rpc_error"
    return "quote_error"


def _error_payload(
    route: Tuple[str, ...],
    amount_in: int,
    *,
    kind: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
    hops: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "route": route,
        "amount_in": int(amount_in),
        "profit": -1,
        "profit_raw": -1,
        "error": {
            "kind": str(kind),
            "reason": str(reason),
            "details": details or {},
        },
        "hops": hops,
    }


def _log_sanity_reject(details: Dict[str, Any]) -> None:
    try:
        route = details.get("route")
        dex_path = details.get("dex_path")
        fee_tiers = details.get("fee_tiers")
        amount_in = details.get("amount_in")
        amount_out = details.get("amount_out")
        adapter = details.get("adapter")
        rpc_url = details.get("rpc_url")
        raw_prefix = details.get("raw_prefix")
        raw_len = details.get("raw_len")
        reason = details.get("reason")
        print(
            "[SANITY] "
            f"reason={reason} route={route} dex_path={dex_path} fee_tiers={fee_tiers} "
            f"amount_in={amount_in} amount_out={amount_out} adapter={adapter} "
            f"rpc={rpc_url} raw_len={raw_len} raw_prefix={raw_prefix}"
        )
    except Exception:
        return


@dataclass(frozen=True)
class Candidate:
    route: Tuple[str, ...]
    amount_in: int
    hops: Optional[Tuple[Hop, ...]] = None


class PriceScanner:
    def __init__(
        self,
        rpc_url: str = None,
        rpc_urls: Optional[List[str]] = None,
        dexes: Optional[List[str]] = None,
        rpc_timeout_s: Optional[float] = None,
        rpc_retry_count: Optional[int] = None,
    ):
        """Create a scanner.

        You can pass either:
          - rpc_url: single URL
          - rpc_urls: list of URLs (load-balanced pool)
        """

        if rpc_urls:
            urls = list(rpc_urls)
        else:
            # env/config fallback (RPC_URLS / RPC_URL)
            urls = get_rpc_urls()
        if rpc_url:
            urls = [rpc_url]

        default_timeout_s = float(
            rpc_timeout_s
            if rpc_timeout_s is not None
            else getattr(config, "RPC_TIMEOUT_S", getattr(config, "RPC_DEFAULT_TIMEOUT_S", 3.0))
        )
        retry_count = int(
            rpc_retry_count
            if rpc_retry_count is not None
            else getattr(config, "RPC_RETRY_COUNT", 1)
        )
        if len(urls) > 1:
            self.rpc = RPCPool(urls, default_timeout_s=default_timeout_s, max_retries_per_call=retry_count)
        else:
            self.rpc = AsyncRPC(urls[0], default_timeout_s=default_timeout_s, max_retries=retry_count)
        raw_dexes = [
            str(d).strip().lower()
            for d in (dexes or getattr(config, "DEXES", ["univ3"]))
            if str(d).strip()
        ]
        allowed = {"univ3", "univ2", "sushiswap", "sushiv2"}
        self.dexes = [d for d in raw_dexes if d in allowed]
        if not self.dexes:
            self.dexes = ["univ3"]

        self.adapters = build_adapters(self.rpc, self.dexes)
        self.dexes = list(self.adapters.keys())

        # Per-block caches
        self._block_tag: Optional[str] = None
        self._gas_price_wei: Optional[int] = None
        self._weth_to_usdc_6: Optional[int] = None

        # cache: (block, dex_id, token_in, token_out, amount_in, params_key) -> QuoteEdge
        self._quote_cache: Dict[Tuple[str, str, str, str, int, Tuple[Tuple[str, Any], ...]], Optional[QuoteEdge]] = {}
        # cache: (block, dex_id, token_in, token_out, params_key) -> True (edge is non-quotable / no pool)
        self._dead_edge: Dict[Tuple[str, str, str, str, Tuple[Tuple[str, Any], ...]], bool] = {}
        # cache: (block, route_tuple, amount_in, params_key) -> payload
        self._payload_cache: Dict[Tuple[str, Tuple[str, ...], int, Tuple[Tuple[str, Any], ...]], Dict[str, Any]] = {}
        # cache: (block, dex_id, token_in, token_out, amount_in, params_key) -> error details
        self._quote_error: Dict[Tuple[str, str, str, str, int, Tuple[Tuple[str, Any], ...]], Dict[str, Any]] = {}

    async def prepare_block(self, block: int | BlockContext) -> None:
        """Warm up per-block values (gas + WETH/USDC) and reset caches."""
        if isinstance(block, BlockContext):
            block_tag = str(block.block_tag)
        else:
            block_tag = hex(int(block))
        if self._block_tag == block_tag:
            return

        self._block_tag = block_tag
        self._gas_price_wei = None
        self._weth_to_usdc_6 = None
        self._quote_cache.clear()
        self._payload_cache.clear()
        self._dead_edge.clear()
        self._quote_error.clear()

        # Warmup (best-effort) — failures are OK
        try:
            gp = await self.rpc.call("eth_gasPrice", [], timeout_s=6.0)
            self._gas_price_wei = int(gp, 16)
        except Exception:
            pass

        try:
            # USDC per 1 WETH (1e18)
            edge = await self.best_edge(
                config.TOKENS["WETH"],
                config.TOKENS["USDC"],
                10**18,
                block=block_tag,
                timeout_s=7.0,
            )
            self._weth_to_usdc_6 = int(edge.amount_out) if edge else None
        except Exception:
            pass

        for adapter in self.adapters.values():
            try:
                prep = getattr(adapter, "prepare_block", None)
                if callable(prep):
                    prep(block_tag)
            except Exception:
                pass

    async def fetch_eth_price_usdc(
        self,
        amount_usdc: int,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Tuple[int, int]:
        """Compatibility helper: quote USDC -> WETH for amount_usdc (USDC smallest units)."""
        block_tag = _require_block_tag(block, block_ctx)
        edge = await self.best_edge(
            config.TOKENS["USDC"],
            config.TOKENS["WETH"],
            int(amount_usdc),
            block=block_tag,
            fee_tiers=fee_tiers,
            timeout_s=timeout_s,
        )
        if not edge:
            return 0, 0
        fee_hint = _edge_fee_tier(edge) or _edge_fee_bps(edge)
        return int(fee_hint), int(edge.amount_out)

    async def _quote_best_cached(
        self,
        dex_id: str,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[QuoteEdge]:
        block_tag = _require_block_tag(block, block_ctx)
        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        dex_id = str(dex_id)
        params_key = _param_key(params)
        cache_enabled = bool(getattr(config, "QUOTE_CACHE_ENABLED", True))
        edge_key = (block_tag, dex_id, str(token_in).lower(), str(token_out).lower(), params_key)
        if cache_enabled and self._dead_edge.get(edge_key):
            return None
        key = (block_tag, dex_id, token_in, token_out, int(amount_in), params_key)
        if cache_enabled and key in self._quote_cache:
            return self._quote_cache[key]
        adapter = self.adapters.get(dex_id)
        if not adapter:
            return None
        try:
            q = await adapter.quote_best(
                token_in,
                token_out,
                int(amount_in),
                block=block_tag,
                timeout_s=timeout_s,
                **(params or {}),
            )
            self._quote_error.pop(key, None)
        except Exception as e:
            self._quote_error[key] = {"reason": _classify_quote_error(e), "error": str(e)}
            q = None
        if cache_enabled:
            self._quote_cache[key] = q
            if not q or int(q.amount_out) == 0:
                self._dead_edge[edge_key] = True
        return q

    async def _quote_many_cached(
        self,
        dex_id: str,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
    ) -> List[QuoteEdge]:
        block_tag = _require_block_tag(block, block_ctx)
        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        dex_id = str(dex_id)
        params_key = _param_key(params)
        cache_enabled = bool(getattr(config, "QUOTE_CACHE_ENABLED", True))
        edge_key = (block_tag, dex_id, str(token_in).lower(), str(token_out).lower(), params_key)
        if cache_enabled and self._dead_edge.get(edge_key):
            return []

        adapter = self.adapters.get(dex_id)
        if not adapter:
            return []

        try:
            edges = await adapter.quote_many(
                token_in,
                token_out,
                int(amount_in),
                block=block_tag,
                timeout_s=timeout_s,
                **(params or {}),
            )
            self._quote_error.pop((block_tag, dex_id, token_in, token_out, int(amount_in), params_key), None)
        except Exception as e:
            self._quote_error[(block_tag, dex_id, token_in, token_out, int(amount_in), params_key)] = {
                "reason": _classify_quote_error(e),
                "error": str(e),
            }
            edges = []

        if not edges:
            if cache_enabled:
                self._dead_edge[edge_key] = True
            return []

        for edge in edges:
            edge_params = {}
            if edge.meta and edge.meta.get("fee_tier") is not None:
                edge_params["fee_tier"] = int(edge.meta.get("fee_tier"))
            edge_key2 = (block_tag, dex_id, token_in, token_out, _param_key(edge_params))
            key2 = (block_tag, dex_id, token_in, token_out, int(amount_in), _param_key(edge_params))
            if cache_enabled:
                if key2 not in self._quote_cache:
                    self._quote_cache[key2] = edge
                if edge.amount_out <= 0:
                    self._dead_edge[edge_key2] = True

        return edges

    async def best_edge(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
        dexes: Optional[List[str]] = None,
    ) -> Optional[QuoteEdge]:
        block_tag = _require_block_tag(block, block_ctx)
        params: Dict[str, Any] = {}
        if fee_tiers:
            params["fee_tiers"] = list(fee_tiers)
        dex_list = list(dexes) if dexes else list(self.adapters.keys())
        tasks: List[Any] = []
        for dex_id in dex_list:
            tasks.append(
                self._quote_best_cached(
                    dex_id,
                    token_in,
                    token_out,
                    int(amount_in),
                    block=block_tag,
                    params=params,
                    timeout_s=timeout_s,
                )
            )
        if not tasks:
            return None
        results = await asyncio.gather(*tasks, return_exceptions=True)
        edges: List[QuoteEdge] = []
        for r in results:
            if isinstance(r, QuoteEdge):
                edges.append(r)
        if not edges:
            return None
        return max(edges, key=lambda x: int(x.amount_out))

    async def quote_edges(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
        dexes: Optional[List[str]] = None,
    ) -> List[QuoteEdge]:
        block_tag = _require_block_tag(block, block_ctx)
        params: Dict[str, Any] = {}
        if fee_tiers:
            params["fee_tiers"] = list(fee_tiers)
        dex_list = list(dexes) if dexes else list(self.adapters.keys())
        tasks: List[Any] = []
        for dex_id in dex_list:
            tasks.append(
                self._quote_many_cached(
                    dex_id,
                    token_in,
                    token_out,
                    int(amount_in),
                    block=block_tag,
                    params=params,
                    timeout_s=timeout_s,
                )
            )
        if not tasks:
            return []
        results = await asyncio.gather(*tasks, return_exceptions=True)
        edges: List[QuoteEdge] = []
        for r in results:
            if isinstance(r, list):
                for e in r:
                    if isinstance(e, QuoteEdge):
                        edges.append(e)
        return edges

    async def estimate_gas_cost_token(
        self,
        gas_units: int,
        token_out: str,
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        timeout_s: float = 7.0,
    ) -> int:
        """Return gas cost in `token_out` smallest units."""
        block_tag = _require_block_tag(block, block_ctx)
        token_out = config.token_address(token_out)
        if self._gas_price_wei is None:
            try:
                gp = await self.rpc.call("eth_gasPrice", [], timeout_s=timeout_s)
                self._gas_price_wei = int(gp, 16)
            except Exception:
                return 0

        gas_cost_wei = int(gas_units) * int(self._gas_price_wei)

        # If token_out is USDC, we can convert directly via cached WETH->USDC.
        if str(token_out).lower() == str(config.TOKENS.get("USDC", "")).lower():
            usdc_per_weth = self._weth_to_usdc_6
            if usdc_per_weth is None:
                try:
                    edge = await self.best_edge(
                        config.TOKENS["WETH"],
                        config.TOKENS["USDC"],
                        10**18,
                        block=block_tag,
                        timeout_s=timeout_s,
                    )
                    usdc_per_weth = int(edge.amount_out) if edge else None
                    self._weth_to_usdc_6 = usdc_per_weth
                except Exception:
                    usdc_per_weth = None
            if not usdc_per_weth:
                return 0
            # token per wei = usdc_per_weth / 1e18
            return int(gas_cost_wei * int(usdc_per_weth) / 10**18)

        # Otherwise: quote 1 WETH -> token_out and scale.
        try:
            edge = await self.best_edge(
                config.TOKENS["WETH"], token_out, 10**18, block=block_tag, timeout_s=timeout_s
            )
            if not edge or edge.amount_out == 0:
                return 0
            token_per_weth = int(edge.amount_out)
            return int(gas_cost_wei * token_per_weth / 10**18)
        except Exception:
            return 0

    async def price_payload(
        self,
        candidate: Dict[str, Any],
        *,
        block: Optional[str] = None,
        block_ctx: Optional[BlockContext] = None,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute amount_out + profit for an N-hop route.

        Returns payload with:
          profit_raw: int in smallest units of token_in
          profit: float in token_in units
        """

        block_tag = _require_block_tag(block, block_ctx)
        amount_in = int(candidate["amount_in"])
        hops = _normalize_hops(candidate.get("hops"))
        tiers = tuple(int(x) for x in (fee_tiers if fee_tiers else config.FEE_TIERS))
        hop_payload = None
        if hops:
            hop_payload = [
                {"token_in": h.token_in, "token_out": h.token_out, "dex_id": h.dex_id, "params": dict(h.params or {})}
                for h in hops
            ]

        if hops:
            tokens: List[str] = []
            ok_chain = True
            for idx, hop in enumerate(hops):
                t_in = config.token_address(hop.token_in)
                t_out = config.token_address(hop.token_out)
                if idx == 0:
                    tokens.append(t_in)
                if tokens and str(tokens[-1]).lower() != str(t_in).lower():
                    ok_chain = False
                    break
                tokens.append(t_out)
            route = tuple(tokens)
            if not ok_chain:
                out = _error_payload(
                    route,
                    amount_in,
                    kind="invalid_route",
                    reason="broken_hops",
                    details={"route": route},
                    hops=hop_payload,
                )
                return out
            params_key = _hops_key(hops)
        else:
            route = tuple(config.token_address(t) for t in candidate["route"])
            params_key = _param_key({"fee_tiers": tiers})

        cache_key = (block_tag, route, amount_in, params_key)
        cache_enabled = bool(getattr(config, "QUOTE_CACHE_ENABLED", True))
        if cache_enabled and cache_key in self._payload_cache:
            return self._payload_cache[cache_key]

        if len(route) < 2:
            out = _error_payload(
                route,
                amount_in,
                kind="invalid_route",
                reason="too_short",
                details={"route": route},
                hops=hop_payload,
            )
            if cache_enabled:
                self._payload_cache[cache_key] = out
            return out

        # sequential quotes
        amt = amount_in
        gas_units = 60_000  # base overhead
        route_dex: List[str] = []
        route_fee_bps: List[int] = []
        route_fee_tier: List[int] = []
        dex_path: List[str] = []
        edge_meta: List[Dict[str, Any]] = []
        hop_amounts: List[Dict[str, Any]] = []
        for i in range(len(route) - 1):
            if hops:
                hop = hops[i]
                q = await self._quote_best_cached(
                    hop.dex_id,
                    hop.token_in,
                    hop.token_out,
                    amt,
                    block=block_tag,
                    params=dict(hop.params or {}),
                    timeout_s=timeout_s,
                )
            else:
                q = await self.best_edge(
                    route[i],
                    route[i + 1],
                    amt,
                    block=block_tag,
                    fee_tiers=list(tiers),
                    timeout_s=timeout_s,
                )

            if not q:
                err_reason = "quote_fail"
                err_details: Dict[str, Any] = {"route": route, "dex_path": list(dex_path)}
                if hops:
                    err_key = (
                        block_tag,
                        str(hop.dex_id),
                        config.token_address(hop.token_in),
                        config.token_address(hop.token_out),
                        int(amt),
                        _param_key(dict(hop.params or {})),
                    )
                    err_info = self._quote_error.get(err_key)
                    if err_info:
                        err_reason = str(err_info.get("reason") or err_reason)
                        err_details["error"] = str(err_info.get("error") or "")
                    err_details["dex_id"] = str(hop.dex_id)
                else:
                    errs = []
                    for dex_id in self.adapters.keys():
                        err_key = (
                            block_tag,
                            str(dex_id),
                            route[i],
                            route[i + 1],
                            int(amt),
                            _param_key({"fee_tiers": list(tiers)}),
                        )
                        err_info = self._quote_error.get(err_key)
                        if err_info and err_info.get("reason"):
                            errs.append(str(err_info.get("reason")))
                    if errs:
                        err_reason = errs[0]
                        err_details["error_reasons"] = errs[:3]
                    err_details["dex_id"] = list(self.adapters.keys())
                out = _error_payload(
                    route,
                    amount_in,
                    kind="quote",
                    reason=err_reason,
                    details=err_details,
                    hops=hop_payload,
                )
                if cache_enabled:
                    self._payload_cache[cache_key] = out
                return out
            if int(q.amount_out) == 0:
                details = {
                    "route": route,
                    "dex_path": list(dex_path),
                    "fee_tiers": list(route_fee_tier),
                    "amount_in": int(amount_in),
                    "amount_out": int(q.amount_out),
                }
                details["dex_id"] = str(q.dex_id)
                out = _error_payload(
                    route,
                    amount_in,
                    kind="sanity",
                    reason="nonsensical_price",
                    details=details,
                    hops=hop_payload,
                )
                _log_sanity_reject({**details, "reason": "nonsensical_price"})
                if cache_enabled:
                    self._payload_cache[cache_key] = out
                return out

            route_dex.append(str(q.dex_id))
            route_fee_bps.append(int(_edge_fee_bps(q)))
            route_fee_tier.append(int(_edge_fee_tier(q)))
            dex_path.append(_edge_label(q))
            edge_meta.append(_edge_meta_snapshot(q))
            hop_amounts.append(
                {
                    "amount_in": int(amt),
                    "amount_out": int(q.amount_out),
                    "dex_id": str(q.dex_id),
                    "fee_tier": int(_edge_fee_tier(q)),
                }
            )
            if _is_nonsensical_amount(amt, int(q.amount_out)):
                last_meta = edge_meta[-1] if edge_meta else {}
                details = {
                    "route": route,
                    "dex_path": list(dex_path),
                    "fee_tiers": list(route_fee_tier),
                    "amount_in": int(amount_in),
                    "amount_out": int(q.amount_out),
                    "adapter": last_meta.get("adapter"),
                    "rpc_url": last_meta.get("rpc_url"),
                    "raw_len": last_meta.get("raw_len"),
                    "raw_prefix": last_meta.get("raw_prefix"),
                }
                details["dex_id"] = str(q.dex_id)
                out = _error_payload(
                    route,
                    amount_in,
                    kind="sanity",
                    reason="nonsensical_price",
                    details=details,
                    hops=hop_payload,
                )
                _log_sanity_reject({**details, "reason": "nonsensical_price"})
                if cache_enabled:
                    self._payload_cache[cache_key] = out
                return out
            amt = int(q.amount_out)
            if q.gas_estimate is not None:
                gas_units += int(q.gas_estimate)
            else:
                if str(q.dex_id) == "univ3":
                    gas_units += int(getattr(config, "V3_GAS_ESTIMATE", 110_000))
                else:
                    gas_units += int(getattr(config, "V2_GAS_ESTIMATE", 90_000))

        gas_units += 25_000  # cushion
        fixed_gas_units = 0
        try:
            fixed_gas_units = int(os.getenv("FIXED_GAS_UNITS", "0") or 0)
        except Exception:
            fixed_gas_units = 0
        if fixed_gas_units and fixed_gas_units > 0:
            gas_units = int(fixed_gas_units)

        gas_off = str(os.getenv("GAS_OFF", "")).strip().lower() in ("1", "true", "yes")
        if gas_off:
            gas_cost_in = 0
        else:
            gas_cost_in = await self.estimate_gas_cost_token(gas_units, route[0], block=block_tag)

        profit_gross_raw = int(amt) - int(amount_in)
        profit_net_raw = int(profit_gross_raw) - int(gas_cost_in)
        dec = _decimals(route[0])
        profit_gross = float(profit_gross_raw) / float(10**dec)
        profit = float(profit_net_raw) / float(10**dec)
        amt_in_f = float(amount_in) / float(10**dec)
        profit_pct_gross = (profit_gross / amt_in_f * 100.0) if amt_in_f > 0 else 0.0
        profit_pct = (profit / amt_in_f * 100.0) if amt_in_f > 0 else 0.0

        # Sanity guard: if something upstream returned garbage (e.g., revert-bytes decoded as values),
        # profit can become astronomically large. Drop such payloads early so UI doesn't show nonsense.
        if abs(profit_net_raw) > 10**30 or abs(profit_pct) > 1e9:
            last_meta = edge_meta[-1] if edge_meta else {}
            details = {
                "route": route,
                "dex_path": list(dex_path),
                "fee_tiers": list(route_fee_tier),
                "amount_in": int(amount_in),
                "amount_out": int(amt),
                "profit_net_raw": int(profit_net_raw),
                "profit_pct": float(profit_pct),
                "adapter": last_meta.get("adapter"),
                "rpc_url": last_meta.get("rpc_url"),
                "raw_len": last_meta.get("raw_len"),
                "raw_prefix": last_meta.get("raw_prefix"),
            }
            details["dex_id"] = str(last_meta.get("dex_id") or "")
            out = _error_payload(
                route,
                amount_in,
                kind="sanity",
                reason="overflow_like",
                details=details,
                hops=hop_payload,
            )
            _log_sanity_reject({**details, "reason": "overflow_like"})
            if cache_enabled:
                self._payload_cache[cache_key] = out
            return out

        payload = {
            "route": route,
            "amount_in": int(amount_in),
            "amount_out": int(amt),
            "gas_cost": int(gas_cost_in),
            "gas_units": int(gas_units),
            "profit_raw_no_gas": int(profit_gross_raw),
            "profit_no_gas": float(profit_gross),
            "profit_pct_no_gas": float(profit_pct_gross),
            "profit_gross_raw": int(profit_gross_raw),
            "profit_gross": float(profit_gross),
            "profit_pct_gross": float(profit_pct_gross),
            "profit_raw": int(profit_net_raw),
            "profit": float(profit),
            "profit_raw_net": int(profit_net_raw),
            "profit_net": float(profit),
            "profit_pct": float(profit_pct),
            "token_in": route[0],
            "token_in_symbol": _sym_by_addr(route[0]),
            "route_dex": tuple(route_dex),
            "route_fee_bps": tuple(route_fee_bps),
            "route_fee_tier": tuple(route_fee_tier),
            "dex_path": tuple(dex_path),
            "hops": hop_payload,
            "hop_amounts": list(hop_amounts),
            "to": "0x0000000000000000000000000000000000000000",
        }
        dex_mix: Dict[str, int] = {}
        for d in route_dex:
            dex_mix[str(d)] = int(dex_mix.get(str(d), 0)) + 1
        payload["dex_mix"] = dex_mix
        payload["hops_count"] = int(len(route) - 1)
        if cache_enabled:
            self._payload_cache[cache_key] = payload
        return payload
