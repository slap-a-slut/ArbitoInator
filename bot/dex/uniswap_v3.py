# bot/dex/uniswap_v3.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, List

from eth_abi import decode, encode
from eth_utils import keccak

from infra.rpc import AsyncRPC
from bot.config import UNISWAP_V3_QUOTER, UNISWAP_V3_QUOTER_V2, FEE_TIERS


def _selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


@dataclass(frozen=True)
class Quote:
    fee: int
    amount_out: int
    gas_estimate: Optional[int] = None
    raw_hex: Optional[str] = None

class UniV3:
    def __init__(self, rpc: AsyncRPC):
        self.rpc = rpc

        # Pre-compute selectors
        self._sel_v1 = _selector("quoteExactInputSingle(address,address,uint24,uint256,uint160)")
        self._sel_v2 = _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")

    async def quote_v1(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
    ) -> Quote:
        """Quoter (v1) quoteExactInputSingle.

        Note: Quoter uses a revert-based mechanism to return data. Our AsyncRPC
        extracts revert data for eth_call.
        """
        params = encode(
            ["address", "address", "uint24", "uint256", "uint160"],
            [token_in, token_out, fee, amount_in, 0],
        )
        data = "0x" + self._sel_v1 + params.hex()
        # Quoter v1 intentionally reverts to return data; allow revert-data passthrough.
        raw = await self.rpc.eth_call(
            UNISWAP_V3_QUOTER,
            data,
            block=block,
            timeout_s=timeout_s,
            allow_revert_data=True,
        )
        return Quote(fee=fee, amount_out=int(raw, 16), gas_estimate=None, raw_hex=str(raw))

    async def quote_v2(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int,
        block: str = "latest",
        *,
        timeout_s: Optional[float] = None,
    ) -> Quote:
        """QuoterV2 quoteExactInputSingle.

        Returns amount_out + gas_estimate.
        """
        # Encode struct QuoteExactInputSingleParams as a tuple
        params = encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in, token_out, amount_in, fee, 0)],
        )
        data = "0x" + self._sel_v2 + params.hex()
        # QuoterV2 returns normally; if it reverts, treat that as an error (do NOT decode revert bytes).
        raw = await self.rpc.eth_call(
            UNISWAP_V3_QUOTER_V2,
            data,
            block=block,
            timeout_s=timeout_s,
            allow_revert_data=False,
        )
        blob = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        # QuoterV2 should return exactly 4 words (128 bytes). If not, treat as invalid.
        if len(blob) < 32 * 4:
            raise RuntimeError(f"QuoterV2 returned short blob: {len(blob)} bytes")
        amount_out, _sqrt_price_after, _ticks_crossed, gas_estimate = decode(
            ["uint256", "uint160", "uint32", "uint256"], blob
        )
        return Quote(fee=fee, amount_out=int(amount_out), gas_estimate=int(gas_estimate), raw_hex=str(raw))

    async def best_quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        block: str = "latest",
        *,
        fee_tiers: Optional[List[int]] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[Quote]:
        """Return the best available quote across fee tiers.

        Prefers QuoterV2 (gasEstimate-aware); falls back to Quoter v1.
        """
        results: list[Quote] = []
        tiers = fee_tiers if fee_tiers else FEE_TIERS
        for fee in tiers:
            try:
                q = await self.quote_v2(token_in, token_out, amount_in, fee, block=block, timeout_s=timeout_s)
                results.append(q)
                continue
            except Exception:
                # fall back to v1
                try:
                    q1 = await self.quote_v1(token_in, token_out, amount_in, fee, block=block, timeout_s=timeout_s)
                    results.append(q1)
                except Exception:
                    pass

        if not results:
            return None

        return max(results, key=lambda x: x.amount_out)
