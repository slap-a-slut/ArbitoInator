from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from mempool.decoders.uniswap_v2 import SELECTORS as V2_SELECTORS
from mempool.decoders.uniswap_v3 import SELECTORS as V3_SELECTORS
from mempool.decoders.universal_router import SELECTORS as UR_SELECTORS


@dataclass(frozen=True)
class RouterInfo:
    name: str
    address: str
    dex_type: str
    supported_selectors: Optional[List[str]] = None


def _selector_list(selectors: Dict[str, object]) -> List[str]:
    return [str(s).lower() for s in selectors.keys()]


ROUTERS: Dict[str, RouterInfo] = {
    "uniswap_v2_router02": RouterInfo(
        name="Uniswap V2 Router02",
        address="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        dex_type="univ2",
        supported_selectors=_selector_list(V2_SELECTORS),
    ),
    "uniswap_v3_swaprouter": RouterInfo(
        name="Uniswap V3 SwapRouter",
        address="0xE592427A0AEce92De3Edee1F18E0157C05861564",
        dex_type="univ3",
        supported_selectors=_selector_list(V3_SELECTORS),
    ),
    "uniswap_v3_swaprouter02": RouterInfo(
        name="Uniswap V3 SwapRouter02",
        address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        dex_type="univ3",
        supported_selectors=_selector_list(V3_SELECTORS),
    ),
    "uniswap_universal_router": RouterInfo(
        name="Uniswap Universal Router",
        address="0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B",
        dex_type="universal",
        supported_selectors=_selector_list(UR_SELECTORS),
    ),
    "sushiswap_router": RouterInfo(
        name="SushiSwap Router",
        address="0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        dex_type="sushi",
        supported_selectors=_selector_list(V2_SELECTORS),
    ),
    "oneinch_v5": RouterInfo(
        name="1inch Aggregation Router v5",
        address="0x1111111254EEB25477B68fb85Ed929f73A960582",
        dex_type="aggregator",
        supported_selectors=None,
    ),
    "zero_x_exchange_proxy": RouterInfo(
        name="0x Exchange Proxy",
        address="0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
        dex_type="aggregator",
        supported_selectors=None,
    ),
}

ROUTER_SETS: Dict[str, List[str]] = {
    "core": [
        "uniswap_v2_router02",
        "sushiswap_router",
        "uniswap_v3_swaprouter",
        "uniswap_v3_swaprouter02",
    ],
    "extended": [
        "uniswap_v2_router02",
        "sushiswap_router",
        "uniswap_v3_swaprouter",
        "uniswap_v3_swaprouter02",
        "uniswap_universal_router",
        "oneinch_v5",
        "zero_x_exchange_proxy",
    ],
}


def _normalize_addr(addr: str) -> str:
    return str(addr).strip().lower()


def router_set_names() -> List[str]:
    return sorted(list(ROUTER_SETS.keys()))


def get_router_set(set_name: str) -> List[RouterInfo]:
    name = str(set_name or "").strip().lower() or "core"
    ids = ROUTER_SETS.get(name) or ROUTER_SETS["core"]
    return [ROUTERS[i] for i in ids if i in ROUTERS]


def build_router_registry(
    *,
    router_set: str,
    extra_addresses: Optional[List[str]] = None,
) -> Dict[str, RouterInfo]:
    registry: Dict[str, RouterInfo] = {}
    for info in get_router_set(router_set):
        registry[_normalize_addr(info.address)] = info
    for addr in extra_addresses or []:
        addr_norm = _normalize_addr(addr)
        if not addr_norm or addr_norm in registry:
            continue
        registry[addr_norm] = RouterInfo(
            name=f"Custom Router {addr_norm[:6]}…{addr_norm[-4:]}",
            address=addr_norm,
            dex_type="custom",
            supported_selectors=None,
        )
    return registry


def list_router_names(registry: Dict[str, RouterInfo]) -> List[str]:
    names = [info.name for info in registry.values()]
    return sorted(list(dict.fromkeys(names)))
