from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass(frozen=True)
class BlockContext:
    block_number: int
    block_tag: str
    block_hash: Optional[str] = None
    pinned_http_endpoint: Optional[str] = None
    created_at: float = field(default_factory=time.time)
