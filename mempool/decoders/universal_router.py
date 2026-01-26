from __future__ import annotations

from typing import Optional

from eth_abi import decode

from mempool.types import PendingTx, DecodedSwap
from mempool.decoders.utils import selector, normalize_address, decode_v3_path


SIG_EXECUTE = "execute(bytes,bytes[],uint256)"
SIG_EXECUTE_NO_DEADLINE = "execute(bytes,bytes[])"

SELECTORS = {
    selector(SIG_EXECUTE): (SIG_EXECUTE, ["bytes", "bytes[]", "uint256"]),
    selector(SIG_EXECUTE_NO_DEADLINE): (SIG_EXECUTE_NO_DEADLINE, ["bytes", "bytes[]"]),
}

CMD_V3_SWAP_EXACT_IN = 0x00
CMD_V3_SWAP_EXACT_OUT = 0x01
CMD_V2_SWAP_EXACT_IN = 0x08
CMD_V2_SWAP_EXACT_OUT = 0x09


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
        commands = b""
        inputs = []
        if sig == SIG_EXECUTE:
            commands = decoded[0]
            inputs = decoded[1]
            deadline = int(decoded[2])
        else:
            commands = decoded[0]
            inputs = decoded[1]

        token_in = None
        token_out = None
        amount_in = None
        amount_out_min = None
        path = None
        fee_tiers = None
        recipient = None

        for idx, raw_cmd in enumerate(bytes(commands or b"")):
            if idx >= len(inputs):
                break
            cmd = int(raw_cmd) & 0x3F
            data_in = inputs[idx]
            if not data_in:
                continue
            if cmd in (CMD_V2_SWAP_EXACT_IN, CMD_V2_SWAP_EXACT_OUT):
                try:
                    rec, amt_a, amt_b, path_raw, _payer = decode(
                        ["address", "uint256", "uint256", "address[]", "bool"],
                        data_in,
                    )
                except Exception:
                    continue
                path_norm = [normalize_address(p) for p in (path_raw or [])]
                if not path_norm:
                    continue
                token_in = path_norm[0]
                token_out = path_norm[-1]
                if cmd == CMD_V2_SWAP_EXACT_IN:
                    amount_in = int(amt_a)
                    amount_out_min = int(amt_b)
                else:
                    amount_in = int(amt_b)
                    amount_out_min = int(amt_a)
                path = path_norm
                recipient = normalize_address(rec)
                break
            if cmd in (CMD_V3_SWAP_EXACT_IN, CMD_V3_SWAP_EXACT_OUT):
                try:
                    rec, amt_a, amt_b, path_bytes, _payer = decode(
                        ["address", "uint256", "uint256", "bytes", "bool"],
                        data_in,
                    )
                except Exception:
                    continue
                tokens, fees = decode_v3_path(path_bytes)
                if not tokens:
                    continue
                token_in = tokens[0]
                token_out = tokens[-1]
                if cmd == CMD_V3_SWAP_EXACT_IN:
                    amount_in = int(amt_a)
                    amount_out_min = int(amt_b)
                else:
                    amount_in = int(amt_b)
                    amount_out_min = int(amt_a)
                path = tokens
                fee_tiers = fees if fees else None
                recipient = normalize_address(rec)
                break

        return DecodedSwap(
            tx_hash=tx.tx_hash,
            kind="universal_router",
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
