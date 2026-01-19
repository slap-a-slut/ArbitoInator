from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class PendingTx:
    tx_hash: str
    from_addr: str
    to_addr: Optional[str]
    input: str
    value: int
    max_fee_per_gas: Optional[int]
    max_priority_fee_per_gas: Optional[int]
    tx_type: Optional[int]
    nonce: Optional[int]
    seen_at_ms: int


@dataclass(frozen=True)
class DecodedSwap:
    tx_hash: str
    kind: str
    router: Optional[str]
    token_in: Optional[str]
    token_out: Optional[str]
    amount_in: Optional[int]
    amount_out_min: Optional[int]
    path: Optional[List[str]]
    fee_tiers: Optional[List[int]]
    recipient: Optional[str]
    deadline: Optional[int]
    seen_at_ms: int


@dataclass(frozen=True)
class Trigger:
    trigger_id: str
    tx_hash: str
    trigger_type: str
    tokens_involved: List[str]
    suggested_routes: Optional[List[List[str]]]
    created_at_ms: int
    amount_in: Optional[int]
    token_in: Optional[str]


@dataclass(frozen=True)
class TriggerResult:
    trigger_id: str
    tx_hash: str
    scheduled: int
    finished: int
    timeouts: int
    best_gross: float
    best_net: float
    best_route_summary: Optional[str]
    outcome: str
    ts: int
    pre_block: Optional[int]
    post_block: Optional[int]
    post_best_net: Optional[float]
