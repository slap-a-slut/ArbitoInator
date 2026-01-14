class SlippageControl:
    def compute_min_out(self, expected, bps=100):
        return expected * (10_000 - bps) // 10_000
