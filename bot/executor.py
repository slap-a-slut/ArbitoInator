# bot/executor.py
import asyncio
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

# ----------------------------
# Заглушка для fork_test.py
def prepare_transaction(payload, from_address):
    """
    Формирует "транзакцию" для теста.
    Пока возвращаем просто словарь.
    """
    tx = {
        "from": from_address,
        "to": payload.get("to", "0x0000000000000000000000000000000000000000"),
        "data": payload,
        "value": payload.get("amount_in", 0),
        "gas": 21000
    }
    return tx

async def send_transaction(w3, tx, account):
    """
    Симуляция отправки транзакции на форке.
    Просто возвращаем mock-receipt.
    """
    print(f"[Executor] Simulating sending tx from {account.address} to {tx['to']}")
    await asyncio.sleep(0.1)  # имитация async
    receipt = {
        "status": 1,
        "transactionHash": "0x" + "deadbeef"*8,
        "gasUsed": tx.get("gas", 21000)
    }
    return receipt
# ----------------------------
# Пример использования
if __name__ == "__main__":
    executor = Executor()
    amount_in = 1_000_000  # USDC 1.0 (6 decimals)
    quote_out = 425000000000000  # 0.000425 WETH в wei
    gas_sim = 1000
    profit = executor.run(("USDC", "WETH", "USDC"), amount_in, quote_out, gas_sim)
    print(f"[Executor] Симулированный профит: {profit}")


