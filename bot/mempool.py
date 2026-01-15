# bot/mempool.py

import asyncio
import random

class MempoolSimulator:
    """
    Игрушечный слушатель pending транзакций
    """

    def __init__(self):
        self.pending_txs = []

    async def generate_tx(self):
        """
        Генерируем случайную pending транзакцию
        """
        tx = {
            "hash": hex(random.randint(1, 1_000_000)),
            "gas_price": random.randint(20_000, 200_000)
        }
        self.pending_txs.append(tx)
        if len(self.pending_txs) > 50:
            self.pending_txs.pop(0)
        return tx

    async def watch(self, callback=None):
        """
        Бесконечный цикл генерации tx
        """
        while True:
            tx = await self.generate_tx()
            if callback:
                callback(tx)
            print(f"[Mempool] new tx → {tx['hash']} gas: {tx['gas_price']}")
            await asyncio.sleep(1)
