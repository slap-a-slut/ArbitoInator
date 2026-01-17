from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DexQuote:
    dex: str
    amount_out: int
    fee_bps: int
    gas_estimate: Optional[int] = None
    fee_tier: Optional[int] = None
