# bot/strategies.py
from bot.config import TOKENS
from bot.utils import get_price_estimate  # вспомогательная функция для расчёта swap

class Strategy:
    """
    Арб-стратегия: можно несколько маршрутов (triangular или прямой)
    Симуляция профита на основе реальных котировок (без транзакций)
    """

    def __init__(self):
        # примеры маршрутов
        self.routes = [
            (TOKENS["USDC"], TOKENS["WETH"], TOKENS["USDC"]),  # треугольник
            (TOKENS["USDC"], TOKENS["DAI"], TOKENS["USDC"]),   # альт-арб
        ]

    def get_routes(self):
        return self.routes

    def calc_profit(self, amount_in: int, route: tuple, w3) -> float:
        """
        amount_in - входной токен в минимальных единицах (wei/USDC units)
        route - tuple токенов
        w3 - Web3 provider для вызова quoter
        Возвращает профит в том же токене, что и amount_in
        """
        out = amount_in
        total_gas_cost = 0

        for i in range(len(route) - 1):
            from_token = route[i]
            to_token = route[i + 1]

            # Получаем estimate выхода через get_price_estimate (имитация swap)
            out_est, gas_est = get_price_estimate(w3, from_token, to_token, out)
            out = out_est
            total_gas_cost += gas_est

        profit = out - amount_in - total_gas_cost
        return profit

# ----------------------------
# Функция для fork_test.py
def compute_payload(candidate, w3):
    """
    Формирует payload для executor.
    Берёт candidate, вычисляет профит через Strategy.calc_profit
    """
    strategy = Strategy()
    profit = strategy.calc_profit(
        amount_in=candidate["amount_in"],
        route=candidate["route"],
        w3=w3
    )
    return {
        "route": candidate["route"],
        "amount_in": candidate["amount_in"],
        "profit": profit,
        "to": "0x0000000000000000000000000000000000000000",  # dummy recipient
    }

# ----------------------------
# Заглушка risk layer
def risk_check(payload):
    """
    Простейший risk check:
    - игнорируем payload с отрицательным профитом
    - можно добавить slippage, MEV detection и т.д.
    """
    if payload["profit"] <= 0:
        return False
    return True
