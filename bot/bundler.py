# bot/bundler.py

from typing import List, Dict

class BundleSimulator:
    """
    Игрушечный Flashbots bundle simulator
    """

    def __init__(self):
        self.bundle = []

    def add_tx(self, tx: Dict):
        """
        tx = {
            "from": "wallet",
            "route": ("USDC","WETH","USDC"),
            "amount_in": 1_000_000,
            "quote_out": 425_000_000_000_000,
            "gas": 1000
        }
        """
        self.bundle.append(tx)

    def clear(self):
        self.bundle = []

    def simulate(self):
        """
        Проходим по всем tx в bundle и суммируем профит
        """
        total_profit = 0
        for tx in self.bundle:
            profit = tx["quote_out"] - tx["amount_in"] - tx["gas"]
            total_profit += profit
        return total_profit

    def summary(self):
        print(f"[BundleSimulator] Total TXs: {len(self.bundle)}")
        print(f"[BundleSimulator] Simulated Total Profit: {self.simulate()/1e6:.6f} USDC")
