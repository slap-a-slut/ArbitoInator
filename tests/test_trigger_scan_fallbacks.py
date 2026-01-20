import pytest

from bot.block_context import BlockContext
from bot import config
import fork_test
from mempool.types import Trigger


class DummyPS:
    dexes = ["univ3", "univ2"]
    rpc = None

    async def price_payload(self, c, *, block_ctx=None, fee_tiers=None, timeout_s=None):
        return {"route": c.get("route", ()), "amount_in": c.get("amount_in", 1), "profit_raw": -1}


def _make_trigger(tokens, token_universe=None):
    return Trigger(
        trigger_id="trg-test",
        tx_hash="0x" + "11" * 32,
        trigger_type="backrun_candidate",
        tokens_involved=tokens,
        token_universe=token_universe,
        suggested_routes=None,
        created_at_ms=0,
        amount_in=None,
        token_in=None,
    )


@pytest.mark.asyncio
async def test_trigger_funnel_diagnostics() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.trigger_connectors = ("",)
        s.trigger_base_fallback_enabled = True
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([], token_universe=[])
        out = await fork_test._scan_trigger(trigger, 0.5)
        assert out.get("zero_candidates_reason") == "no_cycles_generated"
        assert out.get("candidates_raw") == 0
        assert out.get("candidates_after_base_filter") == 0
        assert out.get("zero_candidates_stage") == "hops"
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_base_fallback() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.trigger_connectors = ("",)
        s.trigger_base_fallback_enabled = True
        s.trigger_allow_two_hop_fallback = True
        s.trigger_require_three_hops = True
        s.max_hops = 3
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([config.TOKENS["USDC"]])
        out = await fork_test._scan_trigger(trigger, 0.5)
        assert out.get("base_fallback_used") is True
        assert out.get("base_used") in ("USDT", "DAI")
        assert out.get("candidates_after_base_filter", 0) > 0
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_hop_fallback() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.trigger_connectors = ("",)
        s.trigger_allow_two_hop_fallback = True
        s.trigger_require_three_hops = True
        s.max_hops = 3
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger(["0x0000000000000000000000000000000000000009"])
        out = await fork_test._scan_trigger(trigger, 0.5)
        assert out.get("hop_fallback_used") is True
        assert out.get("hops_attempted") == [3, 2]
        assert out.get("scheduled", 0) >= 1
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_connectors_added() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.trigger_connectors = ("",)
        s.trigger_allow_two_hop_fallback = True
        s.trigger_require_three_hops = True
        s.max_hops = 3
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([config.TOKENS["WETH"]])
        out = await fork_test._scan_trigger(trigger, 0.5)
        usdt_norm = str(config.token_address(config.TOKENS["USDT"])).lower()
        assert usdt_norm in (out.get("connectors_added") or [])
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader
