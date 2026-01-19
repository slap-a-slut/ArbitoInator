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
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    try:
        s = fork_test.Settings()
        s.concurrency = 1
        s.min_first_task_s = 0.05
        s.min_scan_reserve_s = 0.05
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
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
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_schedule_at_least_one_when_guard_triggers() -> None:
    class DummyPS:
        async def price_payload(self, c, *, block_ctx=None, fee_tiers=None, timeout_s=None):
            return {"route": c.get("route", ()), "amount_in": c.get("amount_in", 1), "profit_raw": 0}

    old_ps = fork_test.PS
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    try:
        s = fork_test.Settings()
        s.concurrency = 1
        s.min_first_task_s = 0.05
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        block_ctx = BlockContext(block_number=1, block_tag="0x1")
        candidates = [{"route": ("0x1", "0x2"), "amount_in": 10} for _ in range(2)]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 0.07
        _, _, stats = await fork_test._scan_candidates(
            candidates,
            block_ctx,
            fee_tiers=None,
            timeout_s=1.0,
            deadline_s=deadline,
            keep_all=True,
        )
        assert stats.get("scheduled", 0) >= 1
        assert stats.get("schedule_at_least_one") is True
    finally:
        fork_test.PS = old_ps
        fork_test._load_settings = old_loader


@pytest.mark.asyncio
async def test_reason_if_zero_scheduled_deadline_exhausted() -> None:
    class DummyPS:
        async def price_payload(self, c, *, block_ctx=None, fee_tiers=None, timeout_s=None):
            return {"route": c.get("route", ()), "amount_in": c.get("amount_in", 1), "profit_raw": 0}

    old_ps = fork_test.PS
    old_loader = fork_test._load_settings
    fork_test.PS = DummyPS()
    try:
        s = fork_test.Settings()
        s.concurrency = 1
        fork_test._load_settings = lambda: s  # type: ignore[assignment]
        block_ctx = BlockContext(block_number=1, block_tag="0x1")
        candidates = [{"route": ("0x1", "0x2"), "amount_in": 1}]
        loop = asyncio.get_running_loop()
        deadline = loop.time() - 0.01
        _, _, stats = await fork_test._scan_candidates(
            candidates,
            block_ctx,
            fee_tiers=None,
            timeout_s=0.1,
            deadline_s=deadline,
            keep_all=True,
        )
        assert stats.get("scheduled", 0) == 0
        assert stats.get("reason_if_zero_scheduled") == "stage1_deadline_exhausted_pre_scan"
    finally:
        fork_test.PS = old_ps
        fork_test._load_settings = old_loader
