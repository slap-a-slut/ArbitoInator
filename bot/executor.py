# bot/executor.py

from bot.strategies import Strategy

class Executor:
    """
    Симулятор выполнения арба
    """

    def __init__(self):
        self.strategy = Strategy()

    def run(self, route, amount_in: int, quote_out: int, gas_cost: int = 0):
        """
        route: tuple токенов
        amount_in: входной токен
        quote_out: выход по quoter
        gas_cost: симулируемый gas
        """
        profit = self.strategy.calc_profit(amount_in, quote_out, gas_cost)
        return profit

# Пример использования
if __name__ == "__main__":
    executor = Executor()
    amount_in = 1_000_000  # USDC 1.0 (6 decimals)
    quote_out = 425000000000000  # 0.000425 WETH в wei
    gas_sim = 1000
    profit = executor.run(("USDC", "WETH", "USDC"), amount_in, quote_out, gas_sim)
    print(f"[Executor] Симулированный профит: {profit}")
