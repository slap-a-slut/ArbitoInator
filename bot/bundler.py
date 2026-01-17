# bot/bundler.py

from typing import Dict

from bot import config

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
            "amount_out": 1_001_000,
            "gas_cost": 500,
            "token_in": "USDC"
        }
        """
        self.bundle.append(tx)

    def add_payload(self, payload: Dict):
        """Compatibility helper for PriceScanner payloads."""
        self.add_tx(payload)

    def clear(self):
        self.bundle = []

    def _tx_profit_raw(self, tx: Dict) -> int:
        if "profit_raw" in tx:
            return int(tx.get("profit_raw", 0) or 0)
        amount_out = tx.get("amount_out", tx.get("quote_out", 0) or 0)
        amount_in = tx.get("amount_in", 0) or 0
        gas_cost = tx.get("gas_cost", tx.get("gas", 0) or 0)
        return int(amount_out) - int(amount_in) - int(gas_cost)

    def _tx_token(self, tx: Dict) -> str:
        token = tx.get("token_in")
        if token:
            return str(token)
        route = tx.get("route") or ()
        if route:
            return str(route[0])
        return ""

    def simulate(self) -> Dict[str, int]:
        """Return total profit per token symbol (raw units)."""
        totals: Dict[str, int] = {}
        for tx in self.bundle:
            sym = config.token_symbol(self._tx_token(tx))
            totals[sym] = totals.get(sym, 0) + self._tx_profit_raw(tx)
        return totals

    def summary(self):
        print(f"[BundleSimulator] Total TXs: {len(self.bundle)}")
        totals = self.simulate()
        if not totals:
            print("[BundleSimulator] Simulated Total Profit: 0")
            return
        for sym, profit_raw in totals.items():
            dec = int(config.token_decimals(sym))
            profit = float(profit_raw) / float(10 ** dec) if dec >= 0 else float(profit_raw)
            print(f"[BundleSimulator] Simulated Total Profit: {profit:.6f} {sym}")
