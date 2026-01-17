# bot/scanner.py

"""High-performance scanner for multi-DEX quoting.

This version is designed specifically to:
 - never stall a block forever (timeouts + graceful degradation)
 - minimize RPC usage (per-block caches + gas warmup once per block)
 - support N-hop routes (2-hop, 3-hop, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

from bot import config
from bot.dex.types import DexQuote
from bot.dex.uniswap_v2 import UniV2Like
from bot.dex.uniswap_v3 import UniV3
from infra.rpc import AsyncRPC, RPCPool, get_rpc_urls


def _sym_by_addr(addr: str) -> str:
    return config.token_symbol(addr)


def _decimals(token: str) -> int:
    # Hardcoded decimals for speed (no chain calls)
    return int(config.token_decimals(token))


@dataclass(frozen=True)
class Candidate:
    route: Tuple[str, ...]
    amount_in: int


class PriceScanner:
    def __init__(
        self,
        rpc_url: str = None,
        rpc_urls: Optional[List[str]] = None,
        dexes: Optional[List[str]] = None,
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

        self.rpc = RPCPool(urls) if len(urls) > 1 else AsyncRPC(urls[0])
        raw_dexes = [str(d).strip().lower() for d in (dexes or getattr(config, "DEXES", ["univ3"])) if str(d).strip()]
        allowed = {"univ3", "univ2", "sushiswap"}
        self.dexes = [d for d in raw_dexes if d in allowed]
        if not self.dexes:
            self.dexes = ["univ3"]
        self.univ3 = UniV3(self.rpc) if "univ3" in self.dexes else None
        self.v2_adapters: List[UniV2Like] = []
        if "univ2" in self.dexes:
            self.v2_adapters.append(
                UniV2Like(self.rpc, "univ2", config.UNISWAP_V2_FACTORY, config.UNISWAP_V2_FEE_BPS)
            )
        if "sushiswap" in self.dexes:
            self.v2_adapters.append(
                UniV2Like(self.rpc, "sushiswap", config.SUSHISWAP_FACTORY, config.SUSHISWAP_FEE_BPS)
            )

        # Per-block caches
        self._block_tag: Optional[str] = None
        self._gas_price_wei: Optional[int] = None
        self._weth_to_usdc_6: Optional[int] = None

        # cache: (block, token_in, token_out, amount_in, dexes_key, fee_tiers_key) -> DexQuote
        self._quote_cache: Dict[Tuple[str, str, str, int, Tuple[str, ...], Tuple[int, ...]], Optional[DexQuote]] = {}
        # cache: (block, token_in, token_out, dexes_key, fee_tiers_key) -> True (edge is non-quotable / no pool)
        # This prevents re-trying the same dead edge for different amounts within a block.
        self._dead_edge: Dict[Tuple[str, str, str, Tuple[str, ...], Tuple[int, ...]], bool] = {}
        # cache: (block, route_tuple, amount_in, fee_tiers_key) -> payload
        self._payload_cache: Dict[Tuple[str, Tuple[str, ...], int, Tuple[int, ...]], Dict[str, Any]] = {}

    async def prepare_block(self, block_number: int) -> None:
        """Warm up per-block values (gas + WETH/USDC) and reset caches."""
        block_tag = hex(int(block_number))
        if self._block_tag == block_tag:
            return

        self._block_tag = block_tag
        self._gas_price_wei = None
        self._weth_to_usdc_6 = None
        self._quote_cache.clear()
        self._payload_cache.clear()
        self._dead_edge.clear()

        # Warmup (best-effort) — failures are OK
        try:
            gp = await self.rpc.call("eth_gasPrice", [], timeout_s=6.0)
            self._gas_price_wei = int(gp, 16)
        except Exception:
            pass

        try:
            if self.univ3:
                # USDC per 1 WETH (1e18)
                q = await self.univ3.best_quote(
                    config.TOKENS["WETH"],
                    config.TOKENS["USDC"],
                    10**18,
                    block=block_tag,
                    timeout_s=7.0,
                )
                self._weth_to_usdc_6 = int(q.amount_out) if q else None
        except Exception:
            pass

        for adapter in self.v2_adapters:
            try:
                adapter.prepare_block(block_tag)
            except Exception:
                pass

    async def fetch_eth_price_usdc(
        self,
        amount_usdc: int,
        *,
        block: str = "latest",
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Tuple[int, int]:
        """Compatibility helper: quote USDC -> WETH for amount_usdc (USDC smallest units)."""
        q = await self._best_quote_cached(
            config.TOKENS["USDC"],
            config.TOKENS["WETH"],
            int(amount_usdc),
            block=block,
            fee_tiers=fee_tiers,
            timeout_s=timeout_s,
        )
        if not q:
            return 0, 0
        fee_hint = int(q.fee_tier) if q.fee_tier is not None else int(q.fee_bps)
        return fee_hint, int(q.amount_out)

    async def _best_quote_cached(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[DexQuote]:
        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        tiers = tuple(int(x) for x in (fee_tiers if fee_tiers else config.FEE_TIERS))
        dex_key = tuple(self.dexes)
        edge_key = (block, str(token_in).lower(), str(token_out).lower(), dex_key, tiers)
        if self._dead_edge.get(edge_key):
            return None
        key = (block, token_in, token_out, int(amount_in), dex_key, tiers)
        if key in self._quote_cache:
            return self._quote_cache[key]
        q = await self._best_quote_any_dex(
            token_in,
            token_out,
            int(amount_in),
            block=block,
            fee_tiers=list(tiers),
            timeout_s=timeout_s,
        )
        self._quote_cache[key] = q
        if not q or int(q.amount_out) == 0:
            # Mark edge dead for this block+tiers to avoid retrying on other amounts.
            self._dead_edge[edge_key] = True
        return q

    async def _best_quote_any_dex(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[DexQuote]:
        results: List[DexQuote] = []

        if self.univ3:
            try:
                q = await self.univ3.best_quote(
                    token_in,
                    token_out,
                    int(amount_in),
                    block=block,
                    fee_tiers=fee_tiers,
                    timeout_s=timeout_s,
                )
                if q:
                    fee_bps = int(q.fee) // 100
                    results.append(
                        DexQuote(
                            dex="univ3",
                            amount_out=int(q.amount_out),
                            fee_bps=fee_bps,
                            gas_estimate=q.gas_estimate,
                            fee_tier=int(q.fee),
                        )
                    )
            except Exception:
                pass

        for adapter in self.v2_adapters:
            try:
                q2 = await adapter.quote(token_in, token_out, int(amount_in), block=block, timeout_s=timeout_s)
                if q2:
                    results.append(q2)
            except Exception:
                pass

        if not results:
            return None

        return max(results, key=lambda x: int(x.amount_out))

    async def estimate_gas_cost_token(self, gas_units: int, token_out: str, *, block: str, timeout_s: float = 7.0) -> int:
        """Return gas cost in `token_out` smallest units."""
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
                    q = await self._best_quote_cached(
                        config.TOKENS["WETH"],
                        config.TOKENS["USDC"],
                        10**18,
                        block=block,
                        timeout_s=timeout_s,
                    )
                    usdc_per_weth = int(q.amount_out) if q else None
                    self._weth_to_usdc_6 = usdc_per_weth
                except Exception:
                    usdc_per_weth = None
            if not usdc_per_weth:
                return 0
            # token per wei = usdc_per_weth / 1e18
            return int(gas_cost_wei * int(usdc_per_weth) / 10**18)

        # Otherwise: quote 1 WETH -> token_out and scale.
        try:
            q = await self._best_quote_cached(
                config.TOKENS["WETH"], token_out, 10**18, block=block, timeout_s=timeout_s
            )
            if not q or q.amount_out == 0:
                return 0
            token_per_weth = int(q.amount_out)
            return int(gas_cost_wei * token_per_weth / 10**18)
        except Exception:
            return 0

    async def price_payload(
        self,
        candidate: Dict[str, Any],
        *,
        block: str = "latest",
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Compute amount_out + profit for an N-hop route.

        Returns payload with:
          profit_raw: int in smallest units of token_in
          profit: float in token_in units
        """

        route = tuple(config.token_address(t) for t in candidate["route"])
        amount_in = int(candidate["amount_in"])
        tiers = tuple(int(x) for x in (fee_tiers if fee_tiers else config.FEE_TIERS))
        cache_key = (block, route, amount_in, tiers)
        if cache_key in self._payload_cache:
            return self._payload_cache[cache_key]

        if len(route) < 2:
            out = {"route": route, "amount_in": amount_in, "profit": -1, "profit_raw": -1}
            self._payload_cache[cache_key] = out
            return out

        # sequential quotes
        amt = amount_in
        gas_units = 60_000  # base overhead
        ok = True
        route_dex: List[str] = []
        route_fee_bps: List[int] = []
        route_fee_tier: List[int] = []
        for i in range(len(route) - 1):
            q = await self._best_quote_cached(
                route[i], route[i + 1], amt, block=block, fee_tiers=list(tiers), timeout_s=timeout_s
            )
            if not q or int(q.amount_out) == 0:
                ok = False
                break
            route_dex.append(str(q.dex))
            route_fee_bps.append(int(q.fee_bps))
            route_fee_tier.append(int(q.fee_tier) if q.fee_tier is not None else 0)
            amt = int(q.amount_out)
            if q.gas_estimate is not None:
                gas_units += int(q.gas_estimate)
            else:
                gas_units += 90_000

        if not ok:
            out = {"route": route, "amount_in": amount_in, "profit": -1, "profit_raw": -1}
            self._payload_cache[cache_key] = out
            return out

        gas_units += 25_000  # cushion
        gas_cost_in = await self.estimate_gas_cost_token(gas_units, route[0], block=block)

        profit_raw = int(amt) - int(amount_in) - int(gas_cost_in)
        dec = _decimals(route[0])
        profit = float(profit_raw) / float(10**dec)
        amt_in_f = float(amount_in) / float(10**dec)
        profit_pct = (profit / amt_in_f * 100.0) if amt_in_f > 0 else 0.0

        # Sanity guard: if something upstream returned garbage (e.g., revert-bytes decoded as values),
        # profit can become astronomically large. Drop such payloads early so UI doesn't show nonsense.
        if abs(profit_raw) > 10**30 or abs(profit_pct) > 1e9:
            out = {"route": route, "amount_in": amount_in, "profit": -1, "profit_raw": -1}
            self._payload_cache[cache_key] = out
            return out

        payload = {
            "route": route,
            "amount_in": int(amount_in),
            "amount_out": int(amt),
            "gas_cost": int(gas_cost_in),
            "gas_units": int(gas_units),
            "profit_raw": int(profit_raw),
            "profit": float(profit),
            "profit_pct": float(profit_pct),
            "token_in": route[0],
            "token_in_symbol": _sym_by_addr(route[0]),
            "route_dex": tuple(route_dex),
            "route_fee_bps": tuple(route_fee_bps),
            "route_fee_tier": tuple(route_fee_tier),
            "to": "0x0000000000000000000000000000000000000000",
        }
        self._payload_cache[cache_key] = payload
        return payload
