from __future__ import annotations

from typing import Optional, Tuple

from infra.rpc import RPCPool


async def check_tx_receipt(
    rpc: RPCPool,
    tx_hash: str,
    *,
    timeout_s: float = 2.0,
) -> Tuple[bool, Optional[int]]:
    try:
        receipt = await rpc.call("eth_getTransactionReceipt", [tx_hash], timeout_s=timeout_s)
    except Exception:
        return False, None
    if not receipt:
        return False, None
    block_hex = receipt.get("blockNumber")
    if not block_hex:
        return False, None
    try:
        block_num = int(block_hex, 16)
    except Exception:
        block_num = None
    return True, block_num
