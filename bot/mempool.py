class MempoolWatcher:
    def __init__(self, rpc):
        self.rpc = rpc

    async def listen(self):
        # subscribe to new pending txs (WebSocket)
        pass
