import pytest

from bot import config
from bot.scanner import PriceScanner
from infra.rpc import RPCPool

import fork_test


def test_profit_split_gross_net() -> None:
    s = fork_test.Settings()
    s.slippage_bps = 0.0
    s.mev_buffer_bps = 0.0

    payload = {
        "route": (
            config.TOKENS["USDC"],
            config.TOKENS["WETH"],
            config.TOKENS["USDC"],
        ),
        "amount_in": 1_000_000,   # 1.00 USDC (6 decimals)
        "amount_out": 1_100_000,  # 1.10 USDC
        "gas_cost": 50_000,       # 0.05 USDC
    }

    out = fork_test._apply_safety(payload, s)
    assert int(out.get("profit_gross_raw")) == 100_000
    assert int(out.get("profit_raw_net")) == 50_000


def test_funnel_stages_use_correct_metric() -> None:
    s = fork_test.Settings()
    s.min_profit_abs = 0.0
    s.min_profit_pct = 0.0

    payload = {
        "route": (
            config.TOKENS["USDC"],
            config.TOKENS["WETH"],
            config.TOKENS["USDC"],
        ),
        "amount_in": 1_000_000,
        "amount_out": 1_100_000,
        "gas_cost": 200_000,
        "profit_gross_raw": 100_000,
        "profit_raw_net": -100_000,
        "profit_raw": -100_000,
        "profit_raw_safety": -120_000,
    }

    counts = fork_test._funnel_counts([payload], s)
    assert counts["raw"] == 1
    assert counts["gas"] == 0
    assert counts["safety"] == 0
    assert counts["ready"] == 0


@pytest.mark.asyncio
async def test_block_context_required() -> None:
    ps = PriceScanner(rpc_url="http://example.com")
    with pytest.raises(ValueError):
        await ps.price_payload(
            {"route": (config.TOKENS["USDC"], config.TOKENS["WETH"], config.TOKENS["USDC"]), "amount_in": 1_000_000}
        )


@pytest.mark.asyncio
async def test_rpcpool_get_block_number_mock(monkeypatch) -> None:
    pool = RPCPool(["http://example.com"])

    async def fake_call(method, params, timeout_s=None, allow_revert_data=False):
        assert method == "eth_blockNumber"
        return "0x10"

    monkeypatch.setattr(pool, "call", fake_call)
    block = await pool.get_block_number()
    assert block == 16
