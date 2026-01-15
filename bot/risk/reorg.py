# bot/risk/reorg.py

class ReorgSimulator:
    """
    Мокаем мини-reorg: проверка профита на n последних блоках
    """

    def __init__(self, history_limit: int = 3):
        self.history_limit = history_limit
        self.history = []

    def add_block_profit(self, profit: int):
        self.history.append(profit)
        if len(self.history) > self.history_limit:
            self.history.pop(0)

    def check_stability(self):
        """
        Возвращает True если профит стабилен по последним блокам
        """
        if len(self.history) < 2:
            return True
        return all(p >= 0 for p in self.history)
