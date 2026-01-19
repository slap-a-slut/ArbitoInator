from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from eth_abi import encode as abi_encode
from eth_utils import function_signature_to_4byte_selector, to_checksum_address

from bot import config
from bot.routes import Hop


DEX_ID_V2 = 1
DEX_ID_V3 = 2


def _dex_router(dex_id: str) -> str:
    d = str(dex_id).lower()
    if d in ("univ2", "uniswapv2", "uniswap_v2"):
        return config.UNISWAP_V2_ROUTER
    if d in ("sushiswap", "sushiv2", "sushi", "sushiswap_v2"):
        return config.SUSHISWAP_ROUTER
    if d in ("univ3", "uniswapv3", "uniswap_v3"):
        return config.UNISWAP_V3_SWAP_ROUTER02 or config.UNISWAP_V3_SWAP_ROUTER
    return ""


def _dex_type(dex_id: str) -> int:
    d = str(dex_id).lower()
    if d in ("univ2", "uniswapv2", "uniswap_v2", "sushiswap", "sushiv2", "sushi", "sushiswap_v2"):
        return DEX_ID_V2
    if d in ("univ3", "uniswapv3", "uniswap_v3"):
        return DEX_ID_V3
    raise ValueError(f"unsupported dex_id: {dex_id}")


def _encode_v2_data(router: str, token_in: str, token_out: str) -> bytes:
    path = [to_checksum_address(token_in), to_checksum_address(token_out)]
    return abi_encode(["address", "address[]"], [to_checksum_address(router), path])


def _encode_v3_data(router: str, fee_tier: int) -> bytes:
    return abi_encode(["address", "uint24"], [to_checksum_address(router), int(fee_tier)])


def build_swap_route(hop: Hop, *, amount_in: int) -> Tuple[str, str, int, bytes]:
    dex_type = _dex_type(hop.dex_id)
    router = _dex_router(hop.dex_id)
    if not router:
        raise ValueError(f"missing router for dex_id={hop.dex_id}")
    token_in = to_checksum_address(hop.token_in)
    token_out = to_checksum_address(hop.token_out)
    if dex_type == DEX_ID_V2:
        inner = _encode_v2_data(router, token_in, token_out)
    else:
        fee = int((hop.params or {}).get("fee_tier") or 3000)
        inner = _encode_v3_data(router, fee)
    dex_data = abi_encode(["uint8", "bytes"], [int(dex_type), inner])
    return (token_in, token_out, int(amount_in), dex_data)


def build_execute_call(
    hops: Iterable[Hop],
    *,
    hop_amounts: Iterable[int],
    profit_token: str,
    min_profit: int,
    to_addr: str,
) -> bytes:
    routes: List[Tuple[str, str, int, bytes]] = []
    for hop, amount_in in zip(hops, hop_amounts):
        routes.append(build_swap_route(hop, amount_in=int(amount_in)))
    selector = function_signature_to_4byte_selector(
        "execute((address,address,uint256,bytes)[],uint256,address,address)"
    )
    encoded = abi_encode(
        ["(address,address,uint256,bytes)[]", "uint256", "address", "address"],
        [routes, int(min_profit), to_checksum_address(profit_token), to_checksum_address(to_addr)],
    )
    return bytes(selector) + encoded


def build_execute_call_from_payload(payload: Dict[str, Any], *, min_profit: int, to_addr: str) -> bytes:
    hops_raw = payload.get("hops") or []
    hop_amounts_raw = payload.get("hop_amounts") or []
    if not hops_raw or not hop_amounts_raw or len(hops_raw) != len(hop_amounts_raw):
        raise ValueError("missing hop details for calldata build")
    hops = [
        Hop(
            token_in=str(h.get("token_in")),
            token_out=str(h.get("token_out")),
            dex_id=str(h.get("dex_id")),
            params=dict(h.get("params") or {}),
        )
        for h in hops_raw
    ]
    hop_amounts = [int(x.get("amount_in") or 0) for x in hop_amounts_raw]
    profit_token = str(payload.get("route", [None])[0] or "")
    if not profit_token:
        raise ValueError("missing profit token")
    return build_execute_call(hops, hop_amounts=hop_amounts, profit_token=profit_token, min_profit=min_profit, to_addr=to_addr)
