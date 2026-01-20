from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


def _to_hex(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    try:
        return hex(int(value))
    except Exception:
        return None


def _median(values: Iterable[int]) -> int:
    vals = sorted(int(v) for v in values if v is not None)
    if not vals:
        return 0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return int((vals[mid - 1] + vals[mid]) / 2)


async def get_fee_params(
    rpc: Any,
    *,
    block_count: int = 10,
    reward_percentiles: Optional[list[int]] = None,
    timeout_s: float = 3.0,
) -> Dict[str, int]:
    """Return EIP-1559 fee params from eth_feeHistory (fallback to eth_gasPrice)."""
    if reward_percentiles is None:
        reward_percentiles = [50, 75]

    try:
        res = await rpc.call(
            "eth_feeHistory",
            [hex(int(block_count)), "latest", reward_percentiles],
            timeout_s=timeout_s,
        )
        base_fees = [int(x, 16) for x in (res.get("baseFeePerGas") or []) if isinstance(x, str)]
        rewards = res.get("reward") or []
        idx = len(reward_percentiles) - 1
        priority_vals = []
        for row in rewards:
            if isinstance(row, (list, tuple)) and len(row) > idx and isinstance(row[idx], str):
                priority_vals.append(int(row[idx], 16))
        max_priority = _median(priority_vals) if priority_vals else 0
        base_fee = int(base_fees[-1]) if base_fees else 0
        max_fee = int(base_fee * 2 + max_priority)
        return {
            "base_fee_per_gas": int(base_fee),
            "max_priority_fee_per_gas": int(max_priority),
            "max_fee_per_gas": int(max_fee),
        }
    except Exception:
        pass

    # Fallback: eth_gasPrice
    try:
        gp = await rpc.call("eth_gasPrice", [], timeout_s=timeout_s)
        gas_price = int(gp, 16) if isinstance(gp, str) else int(gp)
        return {
            "base_fee_per_gas": int(gas_price),
            "max_priority_fee_per_gas": int(gas_price),
            "max_fee_per_gas": int(gas_price),
        }
    except Exception:
        return {
            "base_fee_per_gas": 0,
            "max_priority_fee_per_gas": 0,
            "max_fee_per_gas": 0,
        }


async def estimate_gas(
    rpc: Any,
    tx_params: Dict[str, Any],
    *,
    timeout_s: float = 5.0,
) -> int:
    """Estimate gas via eth_estimateGas; accepts int values and converts to hex."""
    if not isinstance(tx_params, dict):
        return 0
    payload = dict(tx_params)
    for key in ("value", "gas", "maxFeePerGas", "maxPriorityFeePerGas", "nonce"):
        if key in payload:
            hx = _to_hex(payload.get(key))
            if hx is not None:
                payload[key] = hx
    try:
        res = await rpc.call("eth_estimateGas", [payload], timeout_s=timeout_s)
        return int(res, 16) if isinstance(res, str) else int(res)
    except Exception:
        return 0
