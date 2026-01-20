from mempool.triggers import build_trigger
from mempool.types import DecodedSwap
from bot import config


def test_trigger_below_usd_threshold() -> None:
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
    assert reason == "below_usd_threshold"


def test_trigger_ok() -> None:
    decoded = DecodedSwap(
        tx_hash="0x2",
        kind="v2_swap",
        router="0xrouter",
        token_in="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        token_out="0x0000000000000000000000000000000000000002",
        amount_in=1_500_000_000,  # 1500 USDC
        amount_out_min=1_400_000_000,
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
    assert trigger.token_universe
    for addr in (
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
    ):
        assert addr in trigger.token_universe


def test_trigger_unknown_token_policy(monkeypatch) -> None:
    decoded = DecodedSwap(
        tx_hash="0x3",
        kind="v2_swap",
        router="0xrouter",
        token_in="0x0000000000000000000000000000000000000009",
        token_out=config.TOKENS.get("WETH"),
        amount_in=10**18,
        amount_out_min=1,
        path=[
            "0x0000000000000000000000000000000000000009",
            config.TOKENS.get("WETH"),
        ],
        fee_tiers=None,
        recipient=None,
        deadline=None,
        seen_at_ms=0,
    )
    monkeypatch.setattr(config, "MEMPOOL_ALLOW_UNKNOWN_TOKENS", True)
    monkeypatch.setattr(config, "MEMPOOL_STRICT_UNKNOWN_TOKENS", False)
    trigger, reason = build_trigger(decoded, min_value_usd=25.0, usd_per_eth=2000.0)
    assert trigger is not None
    assert reason is None
    assert trigger.unknown_value is True

    monkeypatch.setattr(config, "MEMPOOL_ALLOW_UNKNOWN_TOKENS", False)
    trigger, reason = build_trigger(decoded, min_value_usd=25.0, usd_per_eth=2000.0)
    assert trigger is None
    assert reason in ("unknown_value_strict", "missing_amount_in")

    monkeypatch.setattr(config, "MEMPOOL_ALLOW_UNKNOWN_TOKENS", True)
    monkeypatch.setattr(config, "MEMPOOL_STRICT_UNKNOWN_TOKENS", True)
    trigger, reason = build_trigger(decoded, min_value_usd=25.0, usd_per_eth=2000.0)
    assert trigger is None
    assert reason == "unknown_value_strict"


def test_trigger_weth_usd_threshold(monkeypatch) -> None:
    decoded = DecodedSwap(
        tx_hash="0x4",
        kind="v2_swap",
        router="0xrouter",
        token_in=config.TOKENS.get("WETH"),
        token_out="0x000000000000000000000000000000000000000b",
        amount_in=int(0.1 * (10 ** config.token_decimals(config.TOKENS.get("WETH")))),
        amount_out_min=1,
        path=[
            config.TOKENS.get("WETH"),
            "0x000000000000000000000000000000000000000b",
        ],
        fee_tiers=None,
        recipient=None,
        deadline=None,
        seen_at_ms=0,
    )
    monkeypatch.setattr(config, "MEMPOOL_ALLOW_UNKNOWN_TOKENS", True)
    trigger, reason = build_trigger(decoded, min_value_usd=500.0, usd_per_eth=2000.0)
    assert trigger is None
    assert reason == "below_usd_threshold"
