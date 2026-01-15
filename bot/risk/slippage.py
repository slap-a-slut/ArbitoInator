# bot/risk/slippage.py

class SlippageGuard:
    """
    Простая проверка допустимой просадки цены
    """

    def __init__(self, max_slippage_percent: float = 1.0):
        self.max_slippage = max_slippage_percent / 100

    def check(self, amount_in: int, expected_out: int, actual_out: int) -> bool:
        """
        True если слippage допустим, False если слишком большой
        """
        slippage = (expected_out - actual_out) / expected_out
        return slippage <= self.max_slippage
