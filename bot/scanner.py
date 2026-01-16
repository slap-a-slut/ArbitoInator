# bot/scanner.py

"""High-performance scanner for Uniswap V3 quoting.

This version is designed specifically to:
 - never stall a block forever (timeouts + graceful degradation)
 - minimize RPC usage (per-block caches + gas warmup once per block)
 - support N-hop routes (2-hop, 3-hop, etc.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

from bot import config
from bot.dex.uniswap_v3 import Quote, UniV3
from infra.rpc import AsyncRPC


def _sym_by_addr(addr: str) -> str:
    for s, v in config.TOKENS.items():
        if str(v).lower() == str(addr).lower():
            return s
    return str(addr)[:6] + "..." + str(addr)[-4:]


def _decimals(token: str) -> int:
    # Hardcoded decimals for speed (no chain calls)
    t = str(token).lower()
    for sym, addr in config.TOKENS.items():
        if str(addr).lower() == t:
            return int(config.TOKEN_DECIMALS.get(sym, 18))
    return 18


@dataclass(frozen=True)
class Candidate:
    route: Tuple[str, ...]
    amount_in: int


class PriceScanner:
    def __init__(self, rpc_url: str = None):
        url = rpc_url or config.RPC_URL
        self.rpc = AsyncRPC(url)
        self.univ3 = UniV3(self.rpc)

        # Per-block caches
        self._block_tag: Optional[str] = None
        self._gas_price_wei: Optional[int] = None
        self._weth_to_usdc_6: Optional[int] = None

        # cache: (block, token_in, token_out, amount_in, fee_tiers_key) -> Quote
        self._quote_cache: Dict[Tuple[str, str, str, int, Tuple[int, ...]], Optional[Quote]] = {}
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

        # Warmup (best-effort) — failures are OK
        try:
            gp = await self.rpc.call("eth_gasPrice", [], timeout_s=6.0)
            self._gas_price_wei = int(gp, 16)
        except Exception:
            pass

        try:
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

    async def _best_quote_cached(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[Quote]:
        tiers = tuple(int(x) for x in (fee_tiers if fee_tiers else config.FEE_TIERS))
        key = (block, token_in, token_out, int(amount_in), tiers)
        if key in self._quote_cache:
            return self._quote_cache[key]
        q = await self.univ3.best_quote(
            token_in,
            token_out,
            int(amount_in),
            block=block,
            fee_tiers=list(tiers),
            timeout_s=timeout_s,
        )
        self._quote_cache[key] = q
        return q

    async def estimate_gas_cost_token(self, gas_units: int, token_out: str, *, block: str, timeout_s: float = 7.0) -> int:
        """Return gas cost in `token_out` smallest units."""
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

        route = tuple(candidate["route"])
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
        for i in range(len(route) - 1):
            q = await self._best_quote_cached(
                route[i], route[i + 1], amt, block=block, fee_tiers=list(tiers), timeout_s=timeout_s
            )
            if not q or int(q.amount_out) == 0:
                ok = False
                break
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
            "to": "0x0000000000000000000000000000000000000000",
        }
        self._payload_cache[cache_key] = payload
        return payload
