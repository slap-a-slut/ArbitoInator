from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from bot import config
from bot.dex.adapters.uniswap_v2_adapter import UniV2RouterAdapter
from bot.dex.adapters.uniswap_v3_adapter import UniswapV3Adapter
from bot.dex.base import DEXAdapter
from infra.rpc import AsyncRPC


def _clean_dexes(dexes: Optional[Iterable[str]]) -> List[str]:
    out: List[str] = []
    if not dexes:
        return out
    for d in dexes:
        name = str(d).strip().lower()
        if not name:
            continue
        out.append(name)
    return out


def build_adapters(rpc: AsyncRPC, enabled_dexes: Optional[Iterable[str]]) -> Dict[str, DEXAdapter]:
    dexes = _clean_dexes(enabled_dexes)
    if not dexes:
        dexes = ["univ3"]

    adapters: Dict[str, DEXAdapter] = {}
    for dex in dexes:
        if dex == "univ3":
            adapters[dex] = UniswapV3Adapter(rpc)
        elif dex == "univ2":
            adapters[dex] = UniV2RouterAdapter(
                rpc,
                dex_id="univ2",
                router=config.UNISWAP_V2_ROUTER,
                factory=config.UNISWAP_V2_FACTORY,
                fee_bps=config.UNISWAP_V2_FEE_BPS,
                gas_estimate=getattr(config, "V2_GAS_ESTIMATE", 90_000),
            )
        elif dex in ("sushiswap", "sushiv2"):
            adapters[dex] = UniV2RouterAdapter(
                rpc,
                dex_id=dex,
                router=config.SUSHISWAP_ROUTER,
                factory=config.SUSHISWAP_FACTORY,
                fee_bps=config.SUSHISWAP_FEE_BPS,
                gas_estimate=getattr(config, "V2_GAS_ESTIMATE", 90_000),
            )
    return adapters
