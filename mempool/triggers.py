from __future__ import annotations

import time
from typing import List, Optional, Tuple

from mempool.types import DecodedSwap, Trigger
from bot import config


def _is_stable(token_addr: str) -> bool:
    addr = str(token_addr or "").lower()
    return addr in (
        str(config.TOKENS.get("USDC", "")).lower(),
        str(config.TOKENS.get("USDT", "")).lower(),
        str(config.TOKENS.get("DAI", "")).lower(),
    )


def _is_weth(token_addr: str) -> bool:
    return str(token_addr or "").lower() == str(config.TOKENS.get("WETH", "")).lower()


def build_trigger(
    decoded: DecodedSwap,
    *,
    min_value_usd: float,
    usd_per_eth: float,
) -> Tuple[Optional[Trigger], Optional[str]]:
    if not decoded:
        return None, "no_decoded"

    tokens: List[str] = []
    if decoded.path:
        tokens.extend(decoded.path)
    if decoded.token_in:
        tokens.append(decoded.token_in)
    if decoded.token_out:
        tokens.append(decoded.token_out)
    tokens = [t for t in tokens if t]
    if not tokens:
        return None, "unknown_tokens"

    amount_in = decoded.amount_in
    token_in = decoded.token_in
    if amount_in is not None and token_in:
        if _is_stable(token_in):
            if amount_in < int(float(min_value_usd) * (10 ** config.token_decimals(token_in))):
                return None, "below_min_value_usd"
        elif _is_weth(token_in):
            min_eth = float(min_value_usd) / max(1.0, float(usd_per_eth))
            if amount_in < int(min_eth * (10 ** config.token_decimals(token_in))):
                return None, "below_min_value_eth"
    else:
        # Without amount/token we can't apply size threshold.
        pass

    trigger = Trigger(
        trigger_id=f"trg-{decoded.tx_hash[:10]}-{int(time.time() * 1000)}",
        tx_hash=decoded.tx_hash,
        trigger_type="backrun_candidate",
        tokens_involved=sorted(list(dict.fromkeys(tokens))),
        suggested_routes=None,
        created_at_ms=int(time.time() * 1000),
        amount_in=amount_in,
        token_in=token_in,
    )
    return trigger, None
