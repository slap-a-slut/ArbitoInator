from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from mempool.decoders.base import Decoder
from mempool.decoders.uniswap_v2 import UniswapV2Decoder, SELECTORS as V2_SELECTORS
from mempool.decoders.uniswap_v3 import UniswapV3Decoder, SELECTORS as V3_SELECTORS
from mempool.decoders.universal_router import UniversalRouterDecoder, SELECTORS as UR_SELECTORS
from mempool.types import PendingTx, DecodedSwap


def build_router_registry(
    *,
    univ2_routers: Optional[List[str]] = None,
    univ3_routers: Optional[List[str]] = None,
    universal_routers: Optional[List[str]] = None,
) -> Dict[str, str]:
    registry: Dict[str, str] = {}
    for addr in univ2_routers or []:
        registry[str(addr).lower()] = "univ2"
    for addr in univ3_routers or []:
        registry[str(addr).lower()] = "univ3"
    for addr in universal_routers or []:
        registry[str(addr).lower()] = "universal_router"
    return registry


def _router_kind(value: Any) -> str:
    if hasattr(value, "dex_type"):
        try:
            return str(value.dex_type)
        except Exception:
            return ""
    if isinstance(value, dict):
        return str(value.get("dex_type", ""))
    return str(value)


def build_decoders(router_registry: Dict[str, Any]) -> List[Decoder]:
    v2 = {addr for addr, kind in router_registry.items() if _router_kind(kind) in ("univ2", "sushi")}
    v3 = {addr for addr, kind in router_registry.items() if _router_kind(kind) == "univ3"}
    universal = {addr for addr, kind in router_registry.items() if _router_kind(kind) in ("universal", "universal_router")}
    return [
        UniswapV2Decoder(v2),
        UniswapV3Decoder(v3),
        UniversalRouterDecoder(universal),
    ]


def decode_pending_tx(decoders: List[Decoder], tx: PendingTx) -> Tuple[Optional[DecodedSwap], Optional[str]]:
    for decoder in decoders:
        try:
            out = decoder.decode(tx)
        except Exception:
            out = None
        if out:
            return out, None
    return None, "decode_failed"


KNOWN_SELECTORS = set(list(V2_SELECTORS.keys()) + list(V3_SELECTORS.keys()) + list(UR_SELECTORS.keys()))


def is_known_selector(input_data: str) -> bool:
    if not input_data or not input_data.startswith("0x") or len(input_data) < 10:
        return False
    return input_data[2:10].lower() in KNOWN_SELECTORS
