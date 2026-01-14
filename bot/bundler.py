class Bundler:
    def __init__(self, relayer_url):
        self.relayer = relayer_url

    def send(self, tx):
        # submit bundle to Flashbots / MEV relay
        pass
