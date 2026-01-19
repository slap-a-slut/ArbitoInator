from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class QuoteEdge:
    dex_id: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    gas_estimate: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


class DEXAdapter:
    dex_id: str

    async def quote_best(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Optional[QuoteEdge]:
        raise NotImplementedError

    async def quote_many(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        **kwargs: Any,
    ) -> List[QuoteEdge]:
        return []
