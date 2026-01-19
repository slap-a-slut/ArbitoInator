from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Optional

from bot import config
from eth_abi import decode as abi_decode


@dataclass(frozen=True)
class SimResult:
    backend: str
    gross_raw: int
    net_raw: int
    net_after_buffers_raw: int
    gas_units: int
    gas_cost_in: int
    slippage_raw: int
    mev_raw: int
    min_profit_abs_raw: int
    min_profit_pct: float
    net_after_buffers_pct: float
    classification: str


def _to_decimal(val: object) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _scale_amount(token_addr: str, amount_units: object) -> int:
    dec = int(config.token_decimals(token_addr))
    amt = _to_decimal(amount_units)
    if amt is None:
        return 0
    scale = Decimal(10) ** dec
    try:
        return int((amt * scale).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError):
        return 0


def _choose_backend(backends: Optional[list[str]]) -> str:
    if not backends:
        return "quote"
    norm = [str(b).strip().lower() for b in backends if str(b).strip()]
    if "quote" in norm:
        return "quote"
    return "quote_fallback"


def simulate_from_payload(payload: Dict[str, Any], settings: Any, *, backend: str = "quote") -> SimResult:
    amount_in = int(payload.get("amount_in", 0) or 0)
    amount_out = int(payload.get("amount_out", 0) or 0)
    gas_units = int(payload.get("gas_units", 0) or 0)
    gas_cost_in = int(payload.get("gas_cost_in", 0) or 0)

    gross_raw = int(amount_out) - int(amount_in)
    net_raw = int(gross_raw) - int(gas_cost_in)

    slippage_bps = float(getattr(settings, "slippage_bps", 0.0))
    mev_bps = float(getattr(settings, "mev_buffer_bps", getattr(config, "MEV_BUFFER_BPS", 0.0)))
    slip_raw = int(Decimal(amount_in) * Decimal(slippage_bps) / Decimal(10_000))
    mev_raw = int(Decimal(amount_in) * Decimal(mev_bps) / Decimal(10_000))
    net_after_buffers_raw = int(net_raw) - int(slip_raw) - int(mev_raw)

    base_token = payload.get("route", [None])[0]
    min_profit_abs = getattr(settings, "min_profit_abs", 0.0)
    min_profit_abs_raw = _scale_amount(str(base_token), min_profit_abs) if base_token else 0
    min_profit_pct = float(getattr(settings, "min_profit_pct", 0.0))
    net_after_buffers_pct = 0.0
    if amount_in > 0:
        net_after_buffers_pct = float(Decimal(net_after_buffers_raw) / Decimal(amount_in) * Decimal(100))

    classification = "no_hit"
    if gross_raw > 0:
        classification = "gross_hit"
    if net_after_buffers_raw > 0:
        if net_after_buffers_raw >= int(min_profit_abs_raw) and net_after_buffers_pct >= float(min_profit_pct):
            classification = "net_hit"
        else:
            classification = "gross_hit"

    return SimResult(
        backend=str(backend),
        gross_raw=int(gross_raw),
        net_raw=int(net_raw),
        net_after_buffers_raw=int(net_after_buffers_raw),
        gas_units=int(gas_units),
        gas_cost_in=int(gas_cost_in),
        slippage_raw=int(slip_raw),
        mev_raw=int(mev_raw),
        min_profit_abs_raw=int(min_profit_abs_raw),
        min_profit_pct=float(min_profit_pct),
        net_after_buffers_pct=float(net_after_buffers_pct),
        classification=classification,
    )


def simulate_candidate(
    payload: Dict[str, Any],
    settings: Any,
    *,
    backends: Optional[list[str]] = None,
) -> SimResult:
    backend = _choose_backend(backends)
    return simulate_from_payload(payload, settings, backend=backend)


async def simulate_candidate_async(
    payload: Dict[str, Any],
    settings: Any,
    *,
    backends: Optional[list[str]] = None,
    rpc: Optional[Any] = None,
    block_ctx: Optional[Any] = None,
    arb_call: Optional[bytes] = None,
) -> SimResult:
    backend = _choose_backend(backends)
    if backend in ("trace", "state_override", "anvil") and rpc and block_ctx and arb_call:
        addr = getattr(config, "ARB_EXECUTOR_ADDRESS", "")
        if addr:
            try:
                res = await rpc.call(
                    "eth_call",
                    [
                        {"to": str(addr), "data": "0x" + arb_call.hex()},
                        str(getattr(block_ctx, "block_tag", "latest")),
                    ],
                    timeout_s=2.0,
                )
                if res and res != "0x":
                    raw = bytes.fromhex(res[2:] if res.startswith("0x") else res)
                    decoded = abi_decode(["uint256"], raw)
                    profit_raw = int(decoded[0])
                    payload_override = dict(payload)
                    payload_override["amount_out"] = int(payload.get("amount_in", 0)) + profit_raw
                    return simulate_from_payload(payload_override, settings, backend=backend)
            except Exception:
                pass
    return simulate_from_payload(payload, settings, backend="quote")
