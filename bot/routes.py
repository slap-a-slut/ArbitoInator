from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Hop:
    token_in: str
    token_out: str
    dex_id: str
    params: Dict[str, object] = field(default_factory=dict)
