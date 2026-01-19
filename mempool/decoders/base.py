from __future__ import annotations

from typing import Optional, Protocol

from mempool.types import PendingTx, DecodedSwap


class Decoder(Protocol):
    def decode(self, tx: PendingTx) -> Optional[DecodedSwap]:
        """Return DecodedSwap if this decoder can parse tx, else None."""
        raise NotImplementedError
