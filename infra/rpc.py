# infra/rpc.py
import aiohttp
import asyncio
import json
from typing import Any
from web3 import Web3

class AsyncRPC:
    def __init__(self, url: str, timeout: int = 15):
        self.url = url
        self.timeout = timeout
        self._id = 0

    async def call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self.url, json=payload, timeout=self.timeout) as resp:
                data = await resp.json()
                if "error" in data:
                    raise Exception(f"RPC error: {data['error']}")
                return data["result"]

    async def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return await self.call("eth_call", [{
            "to": to,
            "data": data
        }, block])

    async def get_block_number(self) -> int:
        res = await self.call("eth_blockNumber", [])
        return int(res, 16)


# ------------------------
# Добавляем синхронный helper для fork_test.py
def get_provider() -> Web3:
    """
    Возвращает Web3 провайдера для локального форка или любого RPC.
    """
    # пример: локальный fork на http://127.0.0.1:8545
    url = "http://127.0.0.1:8545"
    provider = Web3(Web3.HTTPProvider(url))
    if not provider.is_connected():
        raise ConnectionError(f"Cannot connect to RPC at {url}")
    return provider
