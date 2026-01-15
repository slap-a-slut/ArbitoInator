# bot/risk/mev_guard.py

class MEVGuard:
    """
    Игрушечная проверка MEV угроз:
    - sandwich/fontrun detect
    - mock priority gas auction
    """

    def __init__(self):
        self.recent_txs = []

    def add_tx(self, tx_hash: str, gas_price: int):
        self.recent_txs.append((tx_hash, gas_price))
        if len(self.recent_txs) > 50:
            self.recent_txs.pop(0)

    def check_sandwich_risk(self, my_gas: int) -> bool:
        """
        Простая эвристика: если рядом много tx с gas выше твоего → риск
        """
        for _, g in self.recent_txs:
            if g > my_gas:
                return True
        return False
