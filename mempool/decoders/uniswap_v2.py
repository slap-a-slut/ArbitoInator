from __future__ import annotations

from typing import Optional

from eth_abi import decode

from mempool.types import PendingTx, DecodedSwap
from mempool.decoders.utils import selector, normalize_address


SIG_SWAP_EXACT_TOKENS = "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
SIG_SWAP_TOKENS_FOR_EXACT = "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)"
SIG_SWAP_EXACT_ETH = "swapExactETHForTokens(uint256,address[],address,uint256)"
SIG_SWAP_TOKENS_FOR_EXACT_ETH = "swapTokensForExactETH(uint256,uint256,address[],address,uint256)"
SIG_SWAP_EXACT_TOKENS_FOR_ETH = "swapExactTokensForETH(uint256,uint256,address[],address,uint256)"
SIG_SWAP_ETH_FOR_EXACT = "swapETHForExactTokens(uint256,address[],address,uint256)"

SELECTORS = {
    selector(SIG_SWAP_EXACT_TOKENS): (SIG_SWAP_EXACT_TOKENS, ["uint256", "uint256", "address[]", "address", "uint256"]),
    selector(SIG_SWAP_TOKENS_FOR_EXACT): (SIG_SWAP_TOKENS_FOR_EXACT, ["uint256", "uint256", "address[]", "address", "uint256"]),
    selector(SIG_SWAP_EXACT_ETH): (SIG_SWAP_EXACT_ETH, ["uint256", "address[]", "address", "uint256"]),
    selector(SIG_SWAP_TOKENS_FOR_EXACT_ETH): (SIG_SWAP_TOKENS_FOR_EXACT_ETH, ["uint256", "uint256", "address[]", "address", "uint256"]),
    selector(SIG_SWAP_EXACT_TOKENS_FOR_ETH): (SIG_SWAP_EXACT_TOKENS_FOR_ETH, ["uint256", "uint256", "address[]", "address", "uint256"]),
    selector(SIG_SWAP_ETH_FOR_EXACT): (SIG_SWAP_ETH_FOR_EXACT, ["uint256", "address[]", "address", "uint256"]),
}


class UniswapV2Decoder:
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

        amount_in = None
        amount_out_min = None
        path = None
        recipient = None
        deadline = None

        if sig == SIG_SWAP_EXACT_TOKENS:
            amount_in, amount_out_min, path, recipient, deadline = decoded
        elif sig == SIG_SWAP_TOKENS_FOR_EXACT:
            amount_out, amount_in_max, path, recipient, deadline = decoded
            amount_in = int(amount_in_max)
            amount_out_min = int(amount_out)
        elif sig == SIG_SWAP_EXACT_ETH:
            amount_out_min, path, recipient, deadline = decoded
            amount_in = int(tx.value or 0)
        elif sig == SIG_SWAP_TOKENS_FOR_EXACT_ETH:
            amount_out, amount_in_max, path, recipient, deadline = decoded
            amount_in = int(amount_in_max)
            amount_out_min = int(amount_out)
        elif sig == SIG_SWAP_EXACT_TOKENS_FOR_ETH:
            amount_in, amount_out_min, path, recipient, deadline = decoded
        elif sig == SIG_SWAP_ETH_FOR_EXACT:
            amount_out, path, recipient, deadline = decoded
            amount_in = int(tx.value or 0)
            amount_out_min = int(amount_out)
        else:
            return None

        path_norm = [normalize_address(p) for p in (path or [])]
        token_in = path_norm[0] if path_norm else None
        token_out = path_norm[-1] if path_norm else None

        return DecodedSwap(
            tx_hash=tx.tx_hash,
            kind="v2_swap",
            router=to_addr or None,
            token_in=token_in,
            token_out=token_out,
            amount_in=int(amount_in) if amount_in is not None else None,
            amount_out_min=int(amount_out_min) if amount_out_min is not None else None,
            path=path_norm if path_norm else None,
            fee_tiers=None,
            recipient=normalize_address(recipient) if recipient is not None else None,
            deadline=int(deadline) if deadline is not None else None,
            seen_at_ms=tx.seen_at_ms,
        )
