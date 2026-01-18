from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from eth_abi import decode, encode
from eth_utils import keccak

from bot import config
from bot.dex.types import DexQuote
from infra.rpc import AsyncRPC


def _selector(sig: str) -> str:
    return keccak(text=sig)[:4].hex()


@dataclass(frozen=True)
class Reserves:
    reserve0: int
    reserve1: int


class UniV2Like:
    """Uniswap V2-style adapter (pair reserves)."""

    def __init__(self, rpc: AsyncRPC, name: str, factory: str, fee_bps: int = 30):
        self.rpc = rpc
        self.name = str(name)
        self.factory = config.token_address(factory)
        self.fee_bps = int(fee_bps)

        self._sel_get_pair = _selector("getPair(address,address)")
        self._sel_token0 = _selector("token0()")
        self._sel_token1 = _selector("token1()")
        self._sel_get_reserves = _selector("getReserves()")

        self._pair_cache: Dict[Tuple[str, str], Optional[str]] = {}
        self._token0_cache: Dict[str, Tuple[str, str]] = {}
        self._reserves_cache: Dict[Tuple[str, str], Reserves] = {}
        self._block_tag: Optional[str] = None

    def prepare_block(self, block_tag: str) -> None:
        if self._block_tag == block_tag:
            return
        self._block_tag = block_tag
        self._reserves_cache.clear()

    async def _get_pair(self, token_a: str, token_b: str, *, block: str, timeout_s: Optional[float]) -> Optional[str]:
        a = config.token_address(token_a).lower()
        b = config.token_address(token_b).lower()
        if a == b:
            return None
        key = tuple(sorted([a, b]))
        if key in self._pair_cache:
            return self._pair_cache[key]

        params = encode(["address", "address"], [a, b])
        data = "0x" + self._sel_get_pair + params.hex()
        try:
            raw = await self.rpc.eth_call(self.factory, data, block=block, timeout_s=timeout_s)
            blob = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
            pair = decode(["address"], blob)[0]
            pair = str(pair)
            if pair.lower() == "0x0000000000000000000000000000000000000000":
                pair = None
        except Exception:
            pair = None

        self._pair_cache[key] = pair
        return pair

    async def _get_token0_token1(self, pair: str, *, block: str, timeout_s: Optional[float]) -> Optional[Tuple[str, str]]:
        if pair in self._token0_cache:
            return self._token0_cache[pair]

        try:
            raw0 = await self.rpc.eth_call(pair, "0x" + self._sel_token0, block=block, timeout_s=timeout_s)
            raw1 = await self.rpc.eth_call(pair, "0x" + self._sel_token1, block=block, timeout_s=timeout_s)
            t0 = decode(["address"], bytes.fromhex(raw0[2:] if raw0.startswith("0x") else raw0))[0]
            t1 = decode(["address"], bytes.fromhex(raw1[2:] if raw1.startswith("0x") else raw1))[0]
            out = (str(t0), str(t1))
        except Exception:
            return None

        self._token0_cache[pair] = out
        return out

    async def _get_reserves(self, pair: str, *, block: str, timeout_s: Optional[float]) -> Optional[Reserves]:
        key = (block, pair)
        if key in self._reserves_cache:
            return self._reserves_cache[key]

        try:
            raw = await self.rpc.eth_call(pair, "0x" + self._sel_get_reserves, block=block, timeout_s=timeout_s)
            blob = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
            r0, r1, _ts = decode(["uint112", "uint112", "uint32"], blob)
            reserves = Reserves(int(r0), int(r1))
        except Exception:
            return None

        self._reserves_cache[key] = reserves
        return reserves

    def _amount_out(self, amount_in: int, reserve_in: int, reserve_out: int) -> int:
        if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
            return 0
        fee = max(0, min(10_000, int(self.fee_bps)))
        amt_in_with_fee = int(amount_in) * (10_000 - fee)
        numerator = amt_in_with_fee * int(reserve_out)
        denominator = int(reserve_in) * 10_000 + amt_in_with_fee
        if denominator <= 0:
            return 0
        return int(numerator // denominator)

    async def quote(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        *,
        block: str = "latest",
        timeout_s: Optional[float] = None,
    ) -> Optional[DexQuote]:
        if int(amount_in) <= 0:
            return None

        token_in = config.token_address(token_in)
        token_out = config.token_address(token_out)
        pair = await self._get_pair(token_in, token_out, block=block, timeout_s=timeout_s)
        if not pair:
            return None

        token0_token1 = await self._get_token0_token1(pair, block=block, timeout_s=timeout_s)
        if not token0_token1:
            return None
        token0, token1 = token0_token1

        reserves = await self._get_reserves(pair, block=block, timeout_s=timeout_s)
        if not reserves:
            return None

        if token_in.lower() == token0.lower():
            reserve_in, reserve_out = reserves.reserve0, reserves.reserve1
        else:
            reserve_in, reserve_out = reserves.reserve1, reserves.reserve0

        min_ratio = float(getattr(config, "V2_MIN_RESERVE_RATIO", 0.0))
        if min_ratio > 0.0:
            try:
                if int(reserve_in) < int(amount_in) * float(min_ratio):
                    return None
            except Exception:
                return None

        amount_out = self._amount_out(int(amount_in), reserve_in, reserve_out)
        if amount_out <= 0:
            return None

        max_impact_bps = float(getattr(config, "V2_MAX_PRICE_IMPACT_BPS", 0.0))
        if max_impact_bps > 0.0:
            try:
                ideal_out = (int(amount_in) * int(reserve_out)) / float(reserve_in)
                if ideal_out > 0:
                    impact_bps = max(0.0, (ideal_out - float(amount_out)) / float(ideal_out) * 10_000.0)
                    if impact_bps > float(max_impact_bps):
                        return None
            except Exception:
                return None

        return DexQuote(
            dex=self.name,
            amount_out=int(amount_out),
            fee_bps=int(self.fee_bps),
            gas_estimate=90_000,
        )
