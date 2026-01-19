from types import SimpleNamespace

from bot.simulator import simulate_candidate


def test_simulator_classification_net_hit() -> None:
    settings = SimpleNamespace(slippage_bps=0.0, mev_buffer_bps=0.0, min_profit_abs=0.01, min_profit_pct=0.0)
    payload = {
        "route": ["0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0x0000000000000000000000000000000000000001"],
        "amount_in": 1_000_000,  # 1 USDC
        "amount_out": 1_020_000,  # 1.02 USDC
        "gas_cost_in": 10_000,
    }
    res = simulate_candidate(payload, settings, backends=["quote"])
    assert res.classification == "net_hit"


def test_simulator_classification_gross_hit() -> None:
    settings = SimpleNamespace(slippage_bps=0.0, mev_buffer_bps=0.0, min_profit_abs=0.01, min_profit_pct=0.0)
    payload = {
        "route": ["0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "0x0000000000000000000000000000000000000001"],
        "amount_in": 1_000_000,
        "amount_out": 1_010_000,
        "gas_cost_in": 20_000,
    }
    res = simulate_candidate(payload, settings, backends=["quote"])
    assert res.classification == "gross_hit"
