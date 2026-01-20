import asyncio

from infra import gas as gas_oracle


class FakeRPC:
    def __init__(self, responses):
        self._responses = responses

    async def call(self, method, params, timeout_s=None):
        if method not in self._responses:
            raise RuntimeError(f"missing response for {method}")
        return self._responses[method]


def test_get_fee_params_from_fee_history() -> None:
    rpc = FakeRPC(
        {
            "eth_feeHistory": {
                "baseFeePerGas": ["0x10", "0x20"],
                "reward": [["0x1", "0x7"], ["0x2", "0x8"]],
            }
        }
    )
    res = asyncio.run(gas_oracle.get_fee_params(rpc, block_count=2, reward_percentiles=[50, 75]))
    assert res["base_fee_per_gas"] == 0x20
    assert res["max_priority_fee_per_gas"] == 7
    assert res["max_fee_per_gas"] == (0x20 * 2 + 7)


def test_estimate_gas() -> None:
    rpc = FakeRPC({"eth_estimateGas": "0x5208"})
    res = asyncio.run(gas_oracle.estimate_gas(rpc, {"to": "0x0", "data": "0x"}))
    assert res == 21000
