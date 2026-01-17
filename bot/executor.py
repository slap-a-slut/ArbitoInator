# bot/executor.py
import asyncio
from bot import config
from bot.strategies import Strategy

class Executor:
    """
    Симулятор выполнения арба
    """

    def __init__(self):
        self.strategy = Strategy()

    def run(self, route, amount_in: int, amount_out: int, gas_cost: int = 0):
        """
        route: tuple токенов
        amount_in: входной токен (минимальные единицы token_in)
        amount_out: выходной токен (минимальные единицы token_in)
        gas_cost: симулируемый gas (в минимальных единицах token_in)
        """
        profit_raw = self.strategy.calc_profit(amount_in, amount_out, gas_cost)
        dec = int(config.token_decimals(route[0] if route else ""))
        profit = float(profit_raw) / float(10 ** dec) if dec >= 0 else float(profit_raw)
        return {
            "profit_raw": int(profit_raw),
            "profit": float(profit),
            "token_symbol": config.token_symbol(route[0] if route else ""),
        }

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
    amount_out = 1_001_500  # USDC 1.0015 (6 decimals)
    gas_sim = 500
    result = executor.run((config.TOKENS["USDC"], config.TOKENS["WETH"], config.TOKENS["USDC"]), amount_in, amount_out, gas_sim)
    print(f"[Executor] Симулированный профит: {result['profit']:.6f} {result['token_symbol']}")

