from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import List, Optional, Tuple

from mempool.types import DecodedSwap, Trigger, PendingTx
from bot import config


def _to_decimal(val: object) -> Optional[Decimal]:
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _scale_min_amount(val: object, *, decimals: int) -> Optional[int]:
    d = _to_decimal(val)
    if d is None or d <= 0:
        return None
    scale = Decimal(10) ** int(decimals)
    try:
        return int((d * scale).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _is_stable(token_addr: str) -> bool:
    addr = str(token_addr or "").lower()
    stable_set = getattr(config, "MEMPOOL_STABLE_SET", None)
    if stable_set:
        return addr in {str(x).lower() for x in stable_set}
    return addr in (
        str(config.TOKENS.get("USDC", "")).lower(),
        str(config.TOKENS.get("USDT", "")).lower(),
        str(config.TOKENS.get("DAI", "")).lower(),
    )


def _is_weth(token_addr: str) -> bool:
    weth = getattr(config, "MEMPOOL_WETH", None) or config.TOKENS.get("WETH", "")
    return str(token_addr or "").lower() == str(weth).lower()


def _estimate_usd_value(token_addr: str, amount_in: Optional[int], usd_per_eth: float) -> Optional[Decimal]:
    if amount_in is None:
        return None
    addr = str(token_addr or "").lower()
    if not addr:
        return None
    dec = int(config.token_decimals(addr))
    try:
        base = Decimal(amount_in) / (Decimal(10) ** dec)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if _is_stable(addr):
        return base
    if _is_weth(addr):
        try:
            return base * Decimal(str(usd_per_eth))
        except (InvalidOperation, ValueError, OverflowError):
            return None
    return None


def _format_decimal(val: Decimal) -> str:
    try:
        return format(val.quantize(Decimal("0.0001"), rounding=ROUND_DOWN), "f")
    except (InvalidOperation, ValueError):
        return format(val, "f")


def _min_threshold_raw(token_addr: str) -> Optional[int]:
    addr = str(token_addr or "").lower()
    overrides = {str(k).lower(): v for k, v in (getattr(config, "MEMPOOL_TRIGGER_MIN_BY_TOKEN", {}) or {}).items()}
    if addr in overrides:
        return _scale_min_amount(overrides.get(addr), decimals=config.token_decimals(addr))

    if _is_stable(addr):
        min_stable = getattr(config, "MEMPOOL_TRIGGER_MIN_STABLE", 1000.0)
        return _scale_min_amount(min_stable, decimals=config.token_decimals(addr))
    if _is_weth(addr):
        min_weth = getattr(config, "MEMPOOL_TRIGGER_MIN_WETH", 0.5)
        return _scale_min_amount(min_weth, decimals=config.token_decimals(addr))

    min_unknown = getattr(config, "MEMPOOL_TRIGGER_MIN_UNKNOWN", None)
    if min_unknown is None:
        return None
    return _scale_min_amount(min_unknown, decimals=config.token_decimals(addr))


def _norm_addr(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        addr = config.token_address(token)
    except Exception:
        addr = str(token)
    return str(addr).lower()


def build_trigger(
    decoded: DecodedSwap,
    *,
    min_value_usd: float,
    usd_per_eth: float,
    pending_tx: Optional[PendingTx] = None,
    allow_no_anchor: bool = False,
) -> Tuple[Optional[Trigger], Optional[str]]:
    if not decoded:
        return None, "no_decoded"

    tokens_raw: List[str] = []
    if decoded.path:
        tokens_raw.extend(decoded.path)
    if decoded.token_in:
        tokens_raw.append(decoded.token_in)
    if decoded.token_out:
        tokens_raw.append(decoded.token_out)
    tokens_raw = [t for t in tokens_raw if t]
    tokens: List[str] = []
    for t in tokens_raw:
        t_norm = _norm_addr(t)
        if not t_norm:
            continue
        if t_norm not in tokens:
            tokens.append(t_norm)
    if not tokens:
        return None, "unknown_tokens"

    amount_in = decoded.amount_in
    token_in = _norm_addr(decoded.token_in)
    usd_value: Optional[Decimal] = None
    if token_in:
        usd_value = _estimate_usd_value(token_in, amount_in, float(usd_per_eth))

    connectors = list(getattr(config, "MEMPOOL_TRIGGER_CONNECTORS", []))
    extras = list(getattr(config, "MEMPOOL_TRIGGER_EXTRA_TOKENS", []))
    token_universe: List[str] = []
    for t in tokens + connectors + extras:
        if not t:
            continue
        t_norm = _norm_addr(t)
        if not t_norm:
            continue
        if t_norm not in token_universe:
            token_universe.append(t_norm)

    has_anchor = any(_is_stable(t) or _is_weth(t) for t in tokens)
    has_unknown_token = any(not _is_stable(t) and not _is_weth(t) for t in tokens)

    allow_unknown = bool(getattr(config, "MEMPOOL_ALLOW_UNKNOWN_TOKENS", True))
    strict_unknown = bool(getattr(config, "MEMPOOL_STRICT_UNKNOWN_TOKENS", False))
    raw_min_enabled = bool(getattr(config, "MEMPOOL_RAW_MIN_ENABLED", False))

    if usd_value is not None:
        try:
            if Decimal(usd_value) < Decimal(str(min_value_usd)):
                return None, "below_usd_threshold"
        except (InvalidOperation, ValueError):
            return None, "below_usd_threshold"
    else:
        if strict_unknown:
            return None, "unknown_value_strict"
        if not has_anchor and not allow_no_anchor:
            if amount_in is None:
                return None, "missing_amount_in"
            return None, "unknown_not_in_universe"
        if not allow_unknown:
            return None, "unknown_value_strict"

    if raw_min_enabled and amount_in is not None and token_in:
        min_raw = _min_threshold_raw(token_in)
        if min_raw is not None and int(amount_in) < int(min_raw):
            return None, "below_raw_threshold"

    trigger = Trigger(
        trigger_id=f"trg-{decoded.tx_hash[:10]}-{int(time.time() * 1000)}",
        tx_hash=decoded.tx_hash,
        trigger_type="backrun_candidate",
        tokens_involved=sorted(list(dict.fromkeys(tokens))),
        token_universe=token_universe,
        suggested_routes=None,
        created_at_ms=int(time.time() * 1000),
        amount_in=int(amount_in) if amount_in is not None else None,
        token_in=token_in,
        pending_to=str(pending_tx.to_addr).lower() if pending_tx and pending_tx.to_addr else None,
        pending_input=str(pending_tx.input) if pending_tx and pending_tx.input else None,
        pending_value=int(pending_tx.value) if pending_tx and pending_tx.value is not None else None,
        pending_max_fee_per_gas=int(pending_tx.max_fee_per_gas) if pending_tx and pending_tx.max_fee_per_gas is not None else None,
        pending_max_priority_fee_per_gas=int(pending_tx.max_priority_fee_per_gas) if pending_tx and pending_tx.max_priority_fee_per_gas is not None else None,
        usd_value=_format_decimal(usd_value) if usd_value is not None else None,
        unknown_value=usd_value is None,
        has_unknown_token=bool(has_unknown_token),
    )
    return trigger, None
