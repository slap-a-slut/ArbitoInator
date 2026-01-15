# bot/dex/uniswap_v3.py

from infra.rpc import AsyncRPC
from bot.config import UNISWAP_V3_QUOTER, TOKENS, FEE_TIERS
from eth_abi import encode_abi
from eth_utils import to_hex

class UniV3:
    def __init__(self, rpc: AsyncRPC):
        self.rpc = rpc

    async def quote(self, token_in: str, token_out: str, amount_in: int, fee: int) -> int:
        # quoteExactInputSingle(address,address,uint24,uint256,uint160)
        selector = "0xf7729d43"
        params = encode_abi(
            ["address", "address", "uint24", "uint256", "uint160"],
            [token_in, token_out, fee, amount_in, 0]
        )
        data = selector + params.hex()

        out = await self.rpc.eth_call(UNISWAP_V3_QUOTER, "0x" + data)
        return int(out, 16)

    async def best_quote(self, token_in: str, token_out: str, amount_in: int):
        results = []
        for fee in FEE_TIERS:
            try:
                out = await self.quote(token_in, token_out, amount_in, fee)
                results.append((fee, out))
            except Exception:
                pass

        if not results:
            return None

        return max(results, key=lambda x: x[1])
