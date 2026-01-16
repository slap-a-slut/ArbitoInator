import asyncio
import random

# ----------------------------
# Имитация получения котировки swap
async def simulate_swap(from_token: str, to_token: str, amount_in: int):
    """
    Простая симуляция swap:
    - from_token → to_token
    - amount_in: минимальные единицы токена
    Возвращает: (amount_out, gas_estimate)
    """
    # базовая "цена" (примерно)
    token_prices = {
        "USDC": 1_000_000,  # 1 USDC = 1 unit
        "DAI": 1_000_000,
        "WETH": 1_000_000_000_000_000_000,  # 1 WETH = 1e18 wei
    }

    from_price = token_prices.get(from_token, 1)
    to_price = token_prices.get(to_token, 1)

    # Считаем базовый выход (без слippage)
    amount_out = amount_in * from_price / to_price

    # Добавляем случайный «slippage» ±0.1%
    slippage_factor = random.uniform(0.999, 1.001)
    amount_out *= slippage_factor

    # gas estimate ± случайно 2000-4000 (симуляция)
    gas_estimate = random.randint(2000, 4000)

    # округляем
    amount_out = int(amount_out)
    return amount_out, gas_estimate

# ----------------------------
# Функция для использования в Strategy
def get_price_estimate(w3, from_token: str, to_token: str, amount_in: int):
    """
    Синхронная оболочка для стратегии.
    В реальной версии здесь можно вызывать UniV3 quoter / Curve pool / eth_call
    """
    # для симуляции используем asyncio.run()
    amount_out, gas_est = asyncio.run(simulate_swap(from_token, to_token, amount_in))
    return amount_out, gas_est

# ----------------------------
# Вспомогательные утилиты
def to_wei(amount_eth: float) -> int:
    return int(amount_eth * 1e18)

def from_wei(amount_wei: int) -> float:
    return amount_wei / 1e18
