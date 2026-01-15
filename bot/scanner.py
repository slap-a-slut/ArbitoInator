# bot/scanner.py

import asyncio
from infra.rpc import AsyncRPC
from bot.dex.uniswap_v3 import UniV3
from bot.config import TOKENS, RPC_URL

class PriceScanner:
    def __init__(self):
        self.rpc = AsyncRPC(RPC_URL)
        self.uni = UniV3(self.rpc)

    async def fetch_eth_price_usdc(self, amount_usdc=1_000_000):
        """
        amount_usdc = 1e6 = 1 USDC (6 decimals)
        returns WETH amount
        """
        fee, out = await self.uni.best_quote(
            TOKENS["USDC"],
            TOKENS["WETH"],
            amount_usdc
        )
        return fee, out

async def main():
    scanner = PriceScanner()
    fee, weth_out = await scanner.fetch_eth_price_usdc(1_000_000)

    print(f"[UniV3] USDC → WETH via {fee/10000:.2%} pool = {weth_out/1e18:.8f} WETH")

if __name__ == "__main__":
    asyncio.run(main())
