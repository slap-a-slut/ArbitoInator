import asyncio
import pytest

from bot.block_context import BlockContext
import fork_test


def test_compute_scan_budget_nonzero() -> None:
    out = fork_test._compute_scan_budget_s(5.0, 12.0, min_scan_s=1.0)
    assert out == 1.0


@pytest.mark.asyncio
async def test_scan_candidates_schedules_when_budget_positive() -> None:
    class DummyPS:
        async def price_payload(self, c, *, block_ctx=None, fee_tiers=None, timeout_s=None):
            return {"route": c.get("route", ()), "amount_in": c.get("amount_in", 1), "profit_raw": 0}

    old_ps = fork_test.PS
    fork_test.PS = DummyPS()
    try:
        block_ctx = BlockContext(block_number=1, block_tag="0x1")
        candidates = [{"route": ("0x1", "0x2"), "amount_in": 1} for _ in range(3)]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.5
        payloads, finished, stats = await fork_test._scan_candidates(
            candidates,
            block_ctx,
            fee_tiers=None,
            timeout_s=0.1,
            deadline_s=deadline,
            keep_all=True,
        )
        assert stats.get("scheduled", 0) > 0
        assert finished >= 0
        assert isinstance(payloads, list)
    finally:
        fork_test.PS = old_ps
