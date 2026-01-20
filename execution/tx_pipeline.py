from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from bot import config, preflight
from infra import gas as gas_oracle


LOG_PATH = Path("logs") / "tx_ready.jsonl"


def _hex_data(data: bytes) -> str:
    return "0x" + data.hex()


def _append_jsonl(path: Path, entry: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.open("a", encoding="utf-8").write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _router_for_payload(payload: Dict[str, Any]) -> Optional[str]:
    hops = payload.get("hops") or []
    if not hops:
        return None
    dex_id = str(hops[0].get("dex_id") or "").lower()
    if dex_id in ("univ2", "uniswapv2", "uniswap_v2"):
        return config.UNISWAP_V2_ROUTER
    if dex_id in ("sushiswap", "sushiv2", "sushi", "sushiswap_v2"):
        return config.SUSHISWAP_ROUTER
    if dex_id in ("univ3", "uniswapv3", "uniswap_v3"):
        return config.UNISWAP_V3_SWAP_ROUTER02 or config.UNISWAP_V3_SWAP_ROUTER
    return None


async def build_tx_ready(
    payload: Dict[str, Any],
    settings: Any,
    *,
    rpc: Any,
    block_ctx: Any,
    log_path: Path = LOG_PATH,
) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    candidate_id = payload.get("candidate_id")
    block_tag = getattr(block_ctx, "block_tag", "latest")
    executor_addr = str(getattr(settings, "arb_executor_address", "") or getattr(config, "ARB_EXECUTOR_ADDRESS", ""))
    owner_addr = str(getattr(settings, "arb_executor_owner", "") or getattr(config, "ARB_EXECUTOR_OWNER", ""))
    from_addr = owner_addr or str(getattr(config, "SIM_FROM_ADDRESS", ""))

    entry: Dict[str, Any] = {
        "ts": now_ms,
        "candidate_id": candidate_id,
        "block_tag": str(block_tag),
        "executor_address": executor_addr or None,
        "from": from_addr or None,
        "status": "skipped",
        "drop_reason": None,
    }

    if not executor_addr:
        entry["drop_reason"] = "missing_executor_address"
        _append_jsonl(log_path, entry)
        return entry

    try:
        calldata = preflight.build_arb_calldata(payload, settings, to_addr=from_addr or executor_addr)
    except Exception as exc:
        entry["drop_reason"] = f"calldata_error:{str(exc)[:160]}"
        _append_jsonl(log_path, entry)
        return entry

    entry["arb_calldata_len"] = int(len(calldata))
    entry["arb_calldata_prefix"] = "0x" + calldata.hex()[:16]

    token_in = payload.get("token_in") or (payload.get("route") or [None])[0]
    spender = _router_for_payload(payload) or executor_addr
    ok, reason, balances = await preflight.preflight_checks(
        rpc,
        token_in=str(token_in or ""),
        amount_in=int(payload.get("amount_in", 0) or 0),
        owner=str(executor_addr),
        spender=str(spender),
        block_tag=str(block_tag),
    )
    entry["balances"] = balances
    if not ok:
        entry["drop_reason"] = reason
        _append_jsonl(log_path, entry)
        return entry
    if reason:
        entry["preflight_note"] = reason

    fee_params = await gas_oracle.get_fee_params(rpc, timeout_s=3.0)
    max_fee = int(fee_params.get("max_fee_per_gas", 0) or 0)
    max_priority = int(fee_params.get("max_priority_fee_per_gas", 0) or 0)

    tx_params: Dict[str, Any] = {
        "from": from_addr or executor_addr,
        "to": executor_addr,
        "data": _hex_data(calldata),
        "value": 0,
        "chainId": int(getattr(config, "CHAIN_ID", 1)),
    }

    if max_fee:
        tx_params["maxFeePerGas"] = int(max_fee)
    if max_priority:
        tx_params["maxPriorityFeePerGas"] = int(max_priority)

    gas_est = await gas_oracle.estimate_gas(rpc, tx_params, timeout_s=4.0)
    if gas_est > 0:
        tx_params["gas"] = int(gas_est)

    sim_ok, profit_raw, revert_reason = await preflight.run_eth_call(
        rpc,
        block_tag=str(block_tag),
        from_addr=str(from_addr or executor_addr),
        to_addr=executor_addr,
        data=calldata,
        timeout_s=3.0,
    )

    entry.update(
        {
            "status": "ready" if sim_ok else "reverted",
            "drop_reason": None if sim_ok else "eth_call_revert",
            "sim_ok": bool(sim_ok),
            "sim_revert_reason": revert_reason,
            "profit_raw": str(int(profit_raw)) if profit_raw is not None else None,
            "tx_ready": {
                "from": tx_params.get("from"),
                "to": tx_params.get("to"),
                "data": tx_params.get("data"),
                "value": tx_params.get("value"),
                "chainId": tx_params.get("chainId"),
                "gas": tx_params.get("gas"),
                "maxFeePerGas": tx_params.get("maxFeePerGas"),
                "maxPriorityFeePerGas": tx_params.get("maxPriorityFeePerGas"),
            },
        }
    )
    _append_jsonl(log_path, entry)
    return entry

