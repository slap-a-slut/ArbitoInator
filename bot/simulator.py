from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Optional

from bot import config
from bot import preflight


@dataclass(frozen=True)
class SimResult:
    backend: str
    sim_ok: bool
    sim_revert_reason: Optional[str]
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


def _normalize_backends(backends: Optional[list[str]]) -> list[str]:
    if not backends:
        return ["quote"]
    norm = [str(b).strip().lower() for b in backends if str(b).strip()]
    if not norm:
        return ["quote"]
    if "quote" not in norm:
        norm.append("quote")
    return norm


def simulate_from_payload(
    payload: Dict[str, Any],
    settings: Any,
    *,
    backend: str = "quote",
    sim_ok: bool = True,
    sim_revert_reason: Optional[str] = None,
    force_no_hit: bool = False,
) -> SimResult:
    amount_in = int(payload.get("amount_in", 0) or 0)
    amount_out = int(payload.get("amount_out", 0) or 0)
    gas_units = int(payload.get("gas_units", 0) or 0)
    gas_cost_in = int(payload.get("gas_cost_in", payload.get("gas_cost", 0) or 0) or 0)

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
            classification = "valid_hit"
        else:
            classification = "gross_hit"
    if force_no_hit or not sim_ok:
        classification = "no_hit"

    return SimResult(
        backend=str(backend),
        sim_ok=bool(sim_ok),
        sim_revert_reason=sim_revert_reason,
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
    backends_norm = _normalize_backends(backends)
    return simulate_from_payload(payload, settings, backend=backends_norm[0])


async def simulate_candidate_async(
    payload: Dict[str, Any],
    settings: Any,
    *,
    backends: Optional[list[str]] = None,
    rpc: Optional[Any] = None,
    block_ctx: Optional[Any] = None,
    arb_call: Optional[bytes] = None,
) -> SimResult:
    backends_norm = _normalize_backends(backends)
    last_error: Optional[str] = None
    for backend in backends_norm:
        if backend in ("eth_call", "state_override", "trace", "anvil"):
            if not (rpc and block_ctx and arb_call):
                last_error = "missing_rpc_or_calldata"
                return simulate_from_payload(
                    payload,
                    settings,
                    backend="quote_fallback",
                    sim_ok=False,
                    sim_revert_reason=last_error,
                    force_no_hit=True,
                )
            addr = getattr(config, "ARB_EXECUTOR_ADDRESS", "")
            if not addr:
                last_error = "missing_executor_address"
                return simulate_from_payload(
                    payload,
                    settings,
                    backend="quote_fallback",
                    sim_ok=False,
                    sim_revert_reason=last_error,
                    force_no_hit=True,
                )
            from_addr = getattr(config, "ARB_EXECUTOR_OWNER", "") or getattr(config, "SIM_FROM_ADDRESS", "")
            try:
                sim_ok, profit_raw, reason = await preflight.run_eth_call(
                    rpc,
                    block_tag=str(getattr(block_ctx, "block_tag", "latest")),
                    from_addr=str(from_addr) if from_addr else None,
                    to_addr=str(addr),
                    data=arb_call,
                    timeout_s=2.5,
                )
                if not sim_ok:
                    return simulate_from_payload(
                        payload,
                        settings,
                        backend=backend,
                        sim_ok=False,
                        sim_revert_reason=reason,
                        force_no_hit=True,
                    )
                payload_override = dict(payload)
                if profit_raw is not None:
                    payload_override["amount_out"] = int(payload.get("amount_in", 0)) + int(profit_raw)
                return simulate_from_payload(payload_override, settings, backend=backend, sim_ok=True)
            except Exception as exc:
                last_error = str(exc)[:180]
                return simulate_from_payload(
                    payload,
                    settings,
                    backend=backend,
                    sim_ok=False,
                    sim_revert_reason=last_error,
                    force_no_hit=True,
                )

        if backend == "quote":
            if last_error:
                return simulate_from_payload(
                    payload,
                    settings,
                    backend="quote_fallback",
                    sim_ok=False,
                    sim_revert_reason=last_error,
                    force_no_hit=True,
                )
            return simulate_from_payload(payload, settings, backend="quote", sim_ok=True)

    return simulate_from_payload(payload, settings, backend="quote", sim_ok=True)
