class Executor:
    def __init__(self, contract, bundler):
        self.contract = contract
        self.bundler = bundler

    def build_tx(self, strategy):
        # prepare calldata for Solidity execute()
        pass

    def run(self, bundle):
        # submit bundle / or regular tx
        pass
