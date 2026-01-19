from mempool.triggers import build_trigger
from mempool.types import DecodedSwap


def test_trigger_min_value_usd() -> None:
    decoded = DecodedSwap(
        tx_hash="0x1",
        kind="v2_swap",
        router="0xrouter",
        token_in="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        token_out="0x0000000000000000000000000000000000000001",
        amount_in=1_000_000,  # 1 USDC
        amount_out_min=900_000,
        path=[
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "0x0000000000000000000000000000000000000001",
        ],
        fee_tiers=None,
        recipient=None,
        deadline=None,
        seen_at_ms=0,
    )
    trigger, reason = build_trigger(decoded, min_value_usd=25.0, usd_per_eth=2000.0)
    assert trigger is None
    assert reason == "below_min_value_usd"


def test_trigger_ok() -> None:
    decoded = DecodedSwap(
        tx_hash="0x2",
        kind="v2_swap",
        router="0xrouter",
        token_in="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        token_out="0x0000000000000000000000000000000000000002",
        amount_in=30_000_000,  # 30 USDC
        amount_out_min=28_000_000,
        path=[
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "0x0000000000000000000000000000000000000002",
        ],
        fee_tiers=None,
        recipient=None,
        deadline=None,
        seen_at_ms=0,
    )
    trigger, reason = build_trigger(decoded, min_value_usd=25.0, usd_per_eth=2000.0)
    assert trigger is not None
    assert reason is None
    assert "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in trigger.tokens_involved
