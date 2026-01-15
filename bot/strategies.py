# bot/strategies.py

from bot.config import TOKENS

class Strategy:
    """
    Игрушечная арб-стратегия: USDC ↔ WETH ↔ USDC
    Можно потом добавлять triangular routes
    """

    def __init__(self):
        self.routes = [
            (TOKENS["USDC"], TOKENS["WETH"], TOKENS["USDC"]),
        ]

    def get_routes(self):
        return self.routes

    def calc_profit(self, amount_in: int, out_amount: int, gas_cost: int = 0):
        """
        Простая формула: профит = выход - вход - gas
        amount_in, out_amount должны быть в одинаковых decimals
        """
        return out_amount - amount_in - gas_cost
