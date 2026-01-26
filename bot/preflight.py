from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from eth_abi.abi import decode as abi_decode, encode as abi_encode
from eth_utils.abi import function_signature_to_4byte_selector
from eth_utils.address import to_checksum_address

from bot import config
from bot.arb_builder import build_execute_call_from_payload


_SELECTOR_ERROR = b"\x08\xc3\x79\xa0"
_SELECTOR_PANIC = b"\x4e\x48\x7b\x71"


def _encode_call(signature: str, types: list[str], values: list[Any]) -> str:
    selector = function_signature_to_4byte_selector(signature)
    encoded = abi_encode(types, values)
    return "0x" + (selector + encoded).hex()


def decode_revert_reason(data_hex: str) -> Optional[str]:
    if not data_hex or data_hex == "0x":
        return None
    hx = data_hex[2:] if data_hex.startswith("0x") else data_hex
    try:
        raw = bytes.fromhex(hx)
    except Exception:
        return None
    if raw.startswith(_SELECTOR_ERROR):
        try:
            reason = abi_decode(["string"], raw[4:])[0]
            return f"revert:{reason}"
        except Exception:
            return "revert:error"
    if raw.startswith(_SELECTOR_PANIC):
        try:
            code = abi_decode(["uint256"], raw[4:])[0]
            return f"panic:0x{int(code):x}"
        except Exception:
            return "panic"
    return None


def _scale_amount(token_addr: str, amount_units: object) -> int:
    dec = int(config.token_decimals(token_addr))
    try:
        amt = Decimal(str(amount_units))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    scale = Decimal(10) ** dec
    try:
        return int((amt * scale).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError):
        return 0


async def run_eth_call(
    rpc: Any,
    *,
    block_tag: str,
    from_addr: Optional[str],
    to_addr: str,
    data: bytes,
    value: int = 0,
    timeout_s: float = 3.0,
) -> Tuple[bool, Optional[int], Optional[str]]:
    params: Dict[str, Any] = {
        "to": str(to_addr),
        "data": "0x" + data.hex(),
    }
    if from_addr:
        params["from"] = str(from_addr)
    if value:
        params["value"] = int(value)
    try:
        res = await rpc.call(
            "eth_call",
            [params, str(block_tag)],
            timeout_s=timeout_s,
            allow_revert_data=True,
            allow_error_data=True,
        )
    except Exception as exc:
        return False, None, str(exc)[:180]
    if not res or res == "0x":
        return False, None, "empty_eth_call_result"
    if isinstance(res, str) and (res.startswith("0x08c379a0") or res.startswith("0x4e487b71")):
        return False, None, decode_revert_reason(res) or "revert"
    try:
        raw = bytes.fromhex(res[2:] if isinstance(res, str) and res.startswith("0x") else str(res))
        if len(raw) < 32:
            return False, None, "short_eth_call_result"
        profit_raw = int(abi_decode(["uint256"], raw[:32])[0])
        return True, profit_raw, None
    except Exception as exc:
        return False, None, f"decode_error:{exc}"


async def balance_of(rpc: Any, *, token: str, owner: str, block_tag: str) -> int:
    if not token or not owner:
        return 0
    data = _encode_call("balanceOf(address)", ["address"], [to_checksum_address(owner)])
    try:
        res = await rpc.call("eth_call", [{"to": str(token), "data": data}, str(block_tag)], timeout_s=3.0)
        return int(res, 16) if isinstance(res, str) else int(res)
    except Exception:
        return 0


async def allowance(rpc: Any, *, token: str, owner: str, spender: str, block_tag: str) -> int:
    if not token or not owner or not spender:
        return 0
    data = _encode_call(
        "allowance(address,address)",
        ["address", "address"],
        [to_checksum_address(owner), to_checksum_address(spender)],
    )
    try:
        res = await rpc.call("eth_call", [{"to": str(token), "data": data}, str(block_tag)], timeout_s=3.0)
        return int(res, 16) if isinstance(res, str) else int(res)
    except Exception:
        return 0


async def preflight_checks(
    rpc: Any,
    *,
    token_in: str,
    amount_in: int,
    owner: str,
    spender: str,
    block_tag: str,
) -> Tuple[bool, Optional[str], Dict[str, int]]:
    balances = {
        "balance": 0,
        "allowance": 0,
    }
    if not token_in or not owner:
        return False, "missing_token_or_owner", balances
    balances["balance"] = await balance_of(rpc, token=token_in, owner=owner, block_tag=block_tag)
    if balances["balance"] < int(amount_in):
        return False, "insufficient_balance", balances
    balances["allowance"] = await allowance(rpc, token=token_in, owner=owner, spender=spender, block_tag=block_tag)
    if config.PREFLIGHT_ENFORCE_ALLOWANCE and balances["allowance"] < int(amount_in):
        return False, "insufficient_allowance", balances
    if balances["allowance"] < int(amount_in):
        return True, "allowance_low", balances
    return True, None, balances


def build_arb_calldata(payload: Dict[str, Any], settings: Any, *, to_addr: str) -> bytes:
    slippage_bps = float(getattr(settings, "slippage_bps", 0.0))
    profit_token = None
    route = payload.get("route") or []
    if route:
        profit_token = route[0]
    if not profit_token:
        profit_token = payload.get("token_in")
    min_profit_units = getattr(settings, "min_profit_abs", 0)
    min_profit = _scale_amount(str(profit_token or ""), min_profit_units) if profit_token else int(min_profit_units or 0)
    return build_execute_call_from_payload(
        payload,
        min_profit=min_profit,
        to_addr=to_addr,
        slippage_bps=slippage_bps,
    )
