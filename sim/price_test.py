# sim/price_test.py

import asyncio
from bot import config
from bot.scanner import PriceScanner

async def main():
    scanner = PriceScanner()

    # Полный цикл USDC -> WETH -> USDC с реальными котировками
    route = (config.TOKENS["USDC"], config.TOKENS["WETH"], config.TOKENS["USDC"])
    amount_in = 1_000_000  # USDC 1.0 (6 decimals)
    payload = await scanner.price_payload({"route": route, "amount_in": amount_in})

    amount_out = int(payload.get("amount_out", 0))
    profit = float(payload.get("profit", 0.0))
    profit_pct = float(payload.get("profit_pct", 0.0))
    gas_cost = int(payload.get("gas_cost", 0))
    dex_path = payload.get("route_dex", ())

    print(f"[PriceTest] Amount out: {amount_out/1e6:.6f} USDC")
    if dex_path:
        print(f"[PriceTest] DEX path: {' -> '.join(dex_path)}")
    print(f"[PriceTest] Gas cost: {gas_cost/1e6:.6f} USDC")
    print(f"[PriceTest] Profit: {profit:.6f} USDC ({profit_pct:.4f}%)")

if __name__ == "__main__":
    asyncio.run(main())
