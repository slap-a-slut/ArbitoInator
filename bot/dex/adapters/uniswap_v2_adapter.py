from __future__ import annotations

from typing import Dict, List, Optional

from eth_abi import decode, encode
from eth_utils import keccak

from bot import config
from bot.dex.base import DEXAdapter, QuoteEdge
from bot.dex.uniswap_v2 import UniV2Like
from infra.rpc import AsyncRPC


def _selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


def _raw_fingerprint(raw_hex: Optional[str]) -> Dict[str, Optional[str]]:
    if not raw_hex:
        return {"raw_len": None, "raw_prefix": None}
    hx = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex
    raw_len = len(hx) // 2
    raw_prefix = "0x" + hx[:32] if hx else "0x"
    return {"raw_len": int(raw_len), "raw_prefix": raw_prefix}


class UniV2RouterAdapter(DEXAdapter):
    def __init__(
        self,
        rpc: AsyncRPC,
        *,
        dex_id: str,
        router: str,
        factory: Optional[str] = None,
        fee_bps: int = 30,
        gas_estimate: int = 90_000,
    ):
        self.rpc = rpc
        self.dex_id = str(dex_id)
        self.router = config.token_address(router)
        self.fee_bps = int(fee_bps)
        self.gas_estimate = int(gas_estimate)
        self._sel_get_amounts_out = _selector("getAmountsOut(uint256,address[])")
        self._pair_adapter = None
        if factory:
            self._pair_adapter = UniV2Like(rpc, self.dex_id, factory, fee_bps=self.fee_bps)

    def prepare_block(self, block_tag: str) -> None:
        if self._pair_adapter:
            self._pair_adapter.prepare_block(block_tag)

    async def quote_many(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        **_: object,
    ) -> List[QuoteEdge]:
        if int(amount_in) <= 0:
            return []
        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        if str(token_in).lower() == str(token_out).lower():
            return []

        min_ratio = float(getattr(config, "V2_MIN_RESERVE_RATIO", 0.0))
        max_impact_bps = float(getattr(config, "V2_MAX_PRICE_IMPACT_BPS", 0.0))
        if self._pair_adapter and (min_ratio > 0.0 or max_impact_bps > 0.0):
            try:
                check = await self._pair_adapter.quote(
                    token_in, token_out, int(amount_in), block=block, timeout_s=timeout_s
                )
                if not check:
                    return []
            except Exception:
                return []

        try:
            params = encode(["uint256", "address[]"], [int(amount_in), [token_in, token_out]])
            data = "0x" + self._sel_get_amounts_out + params.hex()
            raw = await self.rpc.eth_call(self.router, data, block=block, timeout_s=timeout_s)
            blob = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
            amounts = decode(["uint256[]"], blob)[0]
            if not amounts or len(amounts) < 2:
                return []
            amount_out = int(amounts[-1])
        except Exception:
            return []

        if amount_out <= 0:
            return []

        return [
            QuoteEdge(
                dex_id=self.dex_id,
                token_in=token_in,
                token_out=token_out,
                amount_in=int(amount_in),
                amount_out=int(amount_out),
                gas_estimate=int(self.gas_estimate),
                meta={
                    "fee_bps": int(self.fee_bps),
                    "adapter": f"{self.dex_id}_router",
                    "rpc_url": getattr(self.rpc, "last_url", None),
                    **_raw_fingerprint(raw),
                },
            )
        ]

    async def quote_best(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
        **kwargs: object,
    ) -> Optional[QuoteEdge]:
        quotes = await self.quote_many(
            token_in,
            token_out,
            amount_in,
            block=block,
            timeout_s=timeout_s,
            **kwargs,
        )
        if not quotes:
            return None
        return quotes[0]
