from __future__ import annotations

from typing import Dict, List, Optional

from bot import config
from bot.dex.base import DEXAdapter, QuoteEdge
from bot.dex.uniswap_v3 import UniV3
from infra.rpc import AsyncRPC


def _raw_fingerprint(raw_hex: Optional[str]) -> Dict[str, Optional[str]]:
    if not raw_hex:
        return {"raw_len": None, "raw_prefix": None}
    hx = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    raw_len = len(hx) // 2
    raw_prefix = "0x" + hx[:32] if hx else "0x"
    return {"raw_len": int(raw_len), "raw_prefix": raw_prefix}


class UniswapV3Adapter(DEXAdapter):
    dex_id = "univ3"

    def __init__(self, rpc: AsyncRPC):
        self.rpc = rpc
        self._v3 = UniV3(rpc)

    async def quote_many(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        fee_tiers: Optional[List[int]] = None,
        fee_tier: Optional[int] = None,
        **_: object,
    ) -> List[QuoteEdge]:
        tiers: List[int]
        if fee_tier is not None:
            tiers = [int(fee_tier)]
        else:
            tiers = [int(x) for x in (fee_tiers if fee_tiers else config.FEE_TIERS)]

        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        amount_in = int(amount_in)

        out: List[QuoteEdge] = []
        for fee in tiers:
            try:
                q = await self._v3.quote_v2(
                    token_in,
                    token_out,
                    amount_in,
                    int(fee),
                    block=block,
                    timeout_s=timeout_s,
                )
                fp = _raw_fingerprint(getattr(q, "raw_hex", None))
                out.append(
                    QuoteEdge(
                        dex_id=self.dex_id,
                        token_in=token_in,
                        token_out=token_out,
                        amount_in=amount_in,
                        amount_out=int(q.amount_out),
                        gas_estimate=int(q.gas_estimate) if q.gas_estimate is not None else None,
                        meta={
                            "fee_tier": int(fee),
                            "fee_bps": int(fee) // 100,
                            "adapter": "univ3_quoter_v2",
                            "raw_len": fp.get("raw_len"),
                            "raw_prefix": fp.get("raw_prefix"),
                            "rpc_url": getattr(self.rpc, "last_url", None),
                        },
                    )
                )
                continue
            except Exception:
                pass

            try:
                q1 = await self._v3.quote_v1(
                    token_in,
                    token_out,
                    amount_in,
                    int(fee),
                    block=block,
                    timeout_s=timeout_s,
                )
                fp = _raw_fingerprint(getattr(q1, "raw_hex", None))
                out.append(
                    QuoteEdge(
                        dex_id=self.dex_id,
                        token_in=token_in,
                        token_out=token_out,
                        amount_in=amount_in,
                        amount_out=int(q1.amount_out),
                        gas_estimate=None,
                        meta={
                            "fee_tier": int(fee),
                            "fee_bps": int(fee) // 100,
                            "adapter": "univ3_quoter_v1",
                            "raw_len": fp.get("raw_len"),
                            "raw_prefix": fp.get("raw_prefix"),
                            "rpc_url": getattr(self.rpc, "last_url", None),
                        },
                    )
                )
            except Exception:
                pass
        return out

    async def quote_best(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        fee_tiers: Optional[List[int]] = None,
        fee_tier: Optional[int] = None,
        **kwargs: object,
    ) -> Optional[QuoteEdge]:
        quotes = await self.quote_many(
            token_in,
            token_out,
            amount_in,
            block=block,
            timeout_s=timeout_s,
            fee_tiers=fee_tiers,
            fee_tier=fee_tier,
            **kwargs,
        )
        if not quotes:
            return None
        return max(quotes, key=lambda q: int(q.amount_out))
