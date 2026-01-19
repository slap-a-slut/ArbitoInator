from __future__ import annotations

from typing import Optional

from eth_abi import decode

from mempool.types import PendingTx, DecodedSwap
from mempool.decoders.utils import selector


SIG_EXECUTE = "execute(bytes,bytes[],uint256)"
SIG_EXECUTE_NO_DEADLINE = "execute(bytes,bytes[])"

SELECTORS = {
    selector(SIG_EXECUTE): (SIG_EXECUTE, ["bytes", "bytes[]", "uint256"]),
    selector(SIG_EXECUTE_NO_DEADLINE): (SIG_EXECUTE_NO_DEADLINE, ["bytes", "bytes[]"]),
}


class UniversalRouterDecoder:
    def __init__(self, router_addresses: Optional[set[str]] = None) -> None:
        self.router_addresses = {a.lower() for a in (router_addresses or set())}

    def decode(self, tx: PendingTx) -> Optional[DecodedSwap]:
        if not tx.input or not tx.input.startswith("0x") or len(tx.input) < 10:
            return None
        to_addr = (tx.to_addr or "").lower()
        selector_hex = tx.input[2:10].lower()
        if selector_hex not in SELECTORS:
            return None
        if self.router_addresses and to_addr not in self.router_addresses:
            return None

        sig, types = SELECTORS[selector_hex]
        data = bytes.fromhex(tx.input[10:])
        try:
            decoded = decode(types, data)
        except Exception:
            return None

        deadline = None
        if sig == SIG_EXECUTE:
            deadline = int(decoded[2])

        return DecodedSwap(
            tx_hash=tx.tx_hash,
            kind="universal_router",
            router=to_addr or None,
            token_in=None,
            token_out=None,
            amount_in=None,
            amount_out_min=None,
            path=None,
            fee_tiers=None,
            recipient=None,
            deadline=deadline,
            seen_at_ms=tx.seen_at_ms,
        )
