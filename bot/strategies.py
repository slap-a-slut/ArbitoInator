# bot/strategies.py

"""Route generation + lightweight risk checks.

We focus on scanning *lots* of plausible triangular cycles without exploding
into completely illiquid pairs.
"""

from __future__ import annotations

from typing import List, Tuple

from bot import config


class Strategy:
    def __init__(self, bases=None):
        # Bases to close the cycle on (profits computed in base token)
        if bases is None:
            bases = [config.TOKENS[s] for s in getattr(config, "STRATEGY_BASES", ["USDC", "USDT", "DAI"])]
        else:
            bases = list(bases)
        self.bases = [config.token_address(t) for t in bases]

        # Liquid universe
        universe_syms = getattr(config, "STRATEGY_UNIVERSE", ["WETH", "WBTC", "LINK", "UNI", "AAVE", "LDO"])
        self.universe = [config.token_address(config.TOKENS[s]) for s in universe_syms if s in config.TOKENS]

        # Hubs usually give more paths
        hub_syms = getattr(config, "STRATEGY_HUBS", ["WETH", "USDC", "USDT", "DAI"])
        self.hubs = [config.token_address(config.TOKENS[s]) for s in hub_syms if s in config.TOKENS]

    @staticmethod
    def calc_profit(amount_in: int, amount_out: int, gas_cost: int = 0) -> int:
        """Return profit in smallest units of the input token."""
        return int(amount_out) - int(amount_in) - int(gas_cost)

    def get_routes(self) -> List[Tuple[str, ...]]:
        routes: List[Tuple[str, ...]] = []

        # 2-hop roundtrips: BASE -> X -> BASE
        for base in self.bases:
            for x in self.universe:
                if x == base:
                    continue
                routes.append((base, x, base))

        # 3-hop triangles: BASE -> A -> B -> BASE
        for base in self.bases:
            tokens = list(dict.fromkeys([base] + self.hubs + self.universe))
            mids = [t for t in tokens if t != base]
            for a in mids:
                for b in mids:
                    if a == b:
                        continue
                    routes.append((base, a, b, base))

        # Deduplicate
        uniq = []
        seen = set()
        for r in routes:
            if r in seen:
                continue
            seen.add(r)
            uniq.append(r)
        return uniq


def risk_check(payload: dict) -> bool:
    """Very lightweight risk filter.

Actual thresholds are applied in fork_test based on UI settings.
Here we only ensure the payload is sane.
"""
    if not payload:
        return False
    if payload.get("profit_raw", -1) <= 0:
        return False
    if payload.get("amount_in", 0) <= 0:
        return False
    return True
