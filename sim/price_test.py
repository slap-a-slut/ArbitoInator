# sim/price_test.py

import asyncio
from bot.scanner import PriceScanner
from bot.executor import Executor

async def main():
    scanner = PriceScanner()
    executor = Executor()

    # Берем quote с реального UniV3
    fee, weth_out = await scanner.fetch_eth_price_usdc(1_000_000)

    # Симуляция выполнения арба
    gas_sim = 1000  # игрушечный gas
    profit = executor.run(("USDC", "WETH", "USDC"), 1_000_000, weth_out, gas_sim)

    print(f"[PriceTest] Quote WETH out: {weth_out/1e18:.8f}")
    print(f"[PriceTest] Симулированный профит: {profit/1e6:.6f} USDC")

if __name__ == "__main__":
    asyncio.run(main())
