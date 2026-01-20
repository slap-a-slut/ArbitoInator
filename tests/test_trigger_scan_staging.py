import time
import pytest

from bot.block_context import BlockContext
from bot import config
import fork_test
from mempool.types import Trigger


class DummyPS:
    dexes = ["univ3", "univ2"]
    rpc = None

    def __init__(self, on_call=None):
        self._on_call = on_call

    async def price_payload(self, c, *, block_ctx=None, fee_tiers=None, timeout_s=None):
        if self._on_call:
            self._on_call()
        return {"route": c.get("route", ()), "amount_in": c.get("amount_in", 1), "profit_raw": 0}


def _make_trigger(tokens):
    return Trigger(
        trigger_id="trg-test",
        tx_hash="0x" + "11" * 32,
        trigger_type="backrun_candidate",
        tokens_involved=tokens,
        token_universe=tokens,
        suggested_routes=None,
        created_at_ms=0,
        amount_in=None,
        token_in=None,
    )


@pytest.mark.asyncio
async def test_schedule_at_least_one_when_candidates_exist() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.trigger_connectors = ("",)
        s.trigger_prepare_budget_ms = 50
        s.min_first_task_s = 0.05
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([config.TOKENS["WETH"]])
        out = await fork_test._scan_trigger(trigger, 0.3)
        assert out.get("scheduled", 0) >= 1
        assert out.get("candidates_raw", 0) > 0
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_prepare_budget_truncation_still_schedules(monkeypatch) -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")

    original_builder = fork_test._build_trigger_routes

    def slow_routes(*args, **kwargs):
        time.sleep(0.02)
        return original_builder(*args, **kwargs)

    try:
        s = fork_test.Settings()
        s.trigger_prepare_budget_ms = 1
        s.trigger_connectors = ("",)
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        monkeypatch.setattr(fork_test, "_build_trigger_routes", slow_routes)
        trigger = _make_trigger([config.TOKENS["WETH"]])
        out = await fork_test._scan_trigger(trigger, 0.3)
        assert out.get("prepare_truncated") is True
        assert out.get("scheduled", 0) >= 1
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_no_rpc_in_prepare_stage() -> None:
    calls = {"count": 0}

    def _bump():
        calls["count"] += 1

    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS(on_call=_bump)
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.min_first_task_s = 0.5
        s.trigger_prepare_budget_ms = 1
        s.trigger_connectors = ("",)
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([config.TOKENS["WETH"]])
        out = await fork_test._scan_trigger(trigger, 0.0)
        assert out.get("scheduled", 0) == 0
        assert calls["count"] == 0
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_zero_schedule_reason_explained() -> None:
    old_ps = fork_test.PS
    old_ctx = fork_test.MEMPOOL_BLOCK_CTX
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    fork_test.MEMPOOL_BLOCK_CTX = BlockContext(block_number=1, block_tag="0x1")
    try:
        s = fork_test.Settings()
        s.min_first_task_s = 0.5
        s.trigger_prepare_budget_ms = 1
        s.trigger_connectors = ("",)
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        trigger = _make_trigger([config.TOKENS["WETH"]])
        out = await fork_test._scan_trigger(trigger, 0.0)
        assert out.get("scheduled", 0) == 0
        assert out.get("zero_schedule_reason") is not None
    finally:
        fork_test.PS = old_ps
        fork_test.MEMPOOL_BLOCK_CTX = old_ctx
        fork_test._load_settings = old_loader
