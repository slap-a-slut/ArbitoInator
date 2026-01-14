class BaseStrategy:
    def __init__(self, pools, tokens):
        self.pools = pools
        self.tokens = tokens

    def compute(self):
        # target: return route candidates + expected profit
        raise NotImplementedError
