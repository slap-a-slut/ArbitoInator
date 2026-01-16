# bot/scanner.py
import asyncio
from bot.config import TOKENS, RPC_URL
from infra.rpc import AsyncRPC
from bot import strategies, utils

# ----------------------------
class ArbScanner:
    """
    Сканер арбитража с реальными котировками.
    - scan_candidates: ищет кандидаты арба
    - fetch_prices: получает реальные котировки через PriceFetcher
    - find_arbitrage_opportunities: возвращает payloads с профитом
    """
    def __init__(self):
        self.rpc = AsyncRPC(RPC_URL)
        self.strategy = strategies.Strategy()

    async def scan_candidates(self):
        """
        Перебираем маршруты из стратегии и задаём amount_in
        """
        candidates = []
        for route in self.strategy.get_routes():
            # amount_in можно потом сделать динамическим
            amount_in = 1_000_000  # 1 USDC (6 decimals)
            candidates.append({
                "route": route,
                "amount_in": amount_in
            })
        return candidates

    async def fetch_prices(self, candidate):
        """
        Берём реальные котировки через PriceFetcher и считаем профит
        """
        route = candidate["route"]
        amount_in = candidate["amount_in"]

        # Первый шаг swap
        out_1 = await self.price_fetcher.get_univ3_quote(route[0], route[1], amount_in)
        # Второй шаг swap
        out_2 = await self.price_fetcher.get_univ3_quote(route[1], route[2], out_1)

        # Симуляция gas cost
        gas_cost = 21000

        profit = self.strategy.calc_profit(amount_in, out_2, gas_cost)

        return {
            "route": route,
            "amount_in": amount_in,
            "profit": profit,
            "to": "0x0000000000000000000000000000000000000000",  # dummy recipient
        }

    async def find_arbitrage_opportunities(self):
        """
        Возвращает список payload’ов с положительным профитом
        """
        candidates = await self.scan_candidates()
        profitable_payloads = []

        for cand in candidates:
            payload = await self.fetch_prices(cand)
            # Простая проверка через risk layer
            if payload["profit"] > 0 and strategies.risk_check(payload):
                profitable_payloads.append(payload)

        return profitable_payloads

# ----------------------------
# Тестовая синхронная обёртка
async def main():
    scanner = ArbScanner()
    opportunities = await scanner.find_arbitrage_opportunities()
    for op in opportunities:
        print(f"Route: {op['route']} | Profit: {op['profit']:.6f}")

if __name__ == "__main__":
    asyncio.run(main())
