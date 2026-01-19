from __future__ import annotations

from typing import Optional

from eth_abi import decode

from mempool.types import PendingTx, DecodedSwap
from mempool.decoders.utils import selector, normalize_address, decode_v3_path


SIG_EXACT_INPUT_SINGLE = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
SIG_EXACT_OUTPUT_SINGLE = "exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
SIG_EXACT_INPUT = "exactInput(bytes,address,uint256,uint256,uint256)"
SIG_EXACT_OUTPUT = "exactOutput(bytes,address,uint256,uint256,uint256)"

SELECTORS = {
    selector(SIG_EXACT_INPUT_SINGLE): (SIG_EXACT_INPUT_SINGLE, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"]),
    selector(SIG_EXACT_OUTPUT_SINGLE): (SIG_EXACT_OUTPUT_SINGLE, ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"]),
    selector(SIG_EXACT_INPUT): (SIG_EXACT_INPUT, ["bytes", "address", "uint256", "uint256", "uint256"]),
    selector(SIG_EXACT_OUTPUT): (SIG_EXACT_OUTPUT, ["bytes", "address", "uint256", "uint256", "uint256"]),
}


class UniswapV3Decoder:
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

        token_in = None
        token_out = None
        amount_in = None
        amount_out_min = None
        path = None
        fee_tiers = None
        recipient = None
        deadline = None

        if sig in (SIG_EXACT_INPUT_SINGLE, SIG_EXACT_OUTPUT_SINGLE):
            params = decoded[0]
            token_in = normalize_address(params[0])
            token_out = normalize_address(params[1])
            fee = int(params[2])
            recipient = normalize_address(params[3])
            deadline = int(params[4])
            amount_in = int(params[5]) if sig == SIG_EXACT_INPUT_SINGLE else int(params[6])
            amount_out_min = int(params[6]) if sig == SIG_EXACT_INPUT_SINGLE else int(params[5])
            path = [token_in, token_out]
            fee_tiers = [fee]
        elif sig == SIG_EXACT_INPUT:
            path_bytes, recipient, deadline, amount_in, amount_out_min = decoded
            tokens, fees = decode_v3_path(path_bytes)
            if tokens:
                token_in = tokens[0]
                token_out = tokens[-1]
                path = tokens
                fee_tiers = fees if fees else None
            recipient = normalize_address(recipient)
            deadline = int(deadline)
            amount_in = int(amount_in)
            amount_out_min = int(amount_out_min)
        elif sig == SIG_EXACT_OUTPUT:
            path_bytes, recipient, deadline, amount_out, amount_in_max = decoded
            tokens, fees = decode_v3_path(path_bytes)
            if tokens:
                token_in = tokens[0]
                token_out = tokens[-1]
                path = tokens
                fee_tiers = fees if fees else None
            recipient = normalize_address(recipient)
            deadline = int(deadline)
            amount_in = int(amount_in_max)
            amount_out_min = int(amount_out)
        else:
            return None

        return DecodedSwap(
            tx_hash=tx.tx_hash,
            kind="v3_swap",
            router=to_addr or None,
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            amount_out_min=amount_out_min,
            path=path,
            fee_tiers=fee_tiers,
            recipient=recipient,
            deadline=deadline,
            seen_at_ms=tx.seen_at_ms,
        )
