import time

import pytest

from infra.rpc import RPCPool, EndpointHealth


def test_health_window_stats() -> None:
    health = EndpointHealth(maxlen=5)
    health.record(ok=True, latency_ms=100.0, reason="ok")
    health.record(ok=False, latency_ms=200.0, reason="timeout")
    health.record(ok=True, latency_ms=150.0, reason="ok")
    stats = health.stats()
    assert stats["count"] == 3
    assert stats["success_rate"] == pytest.approx(2 / 3)
    assert stats["timeout_rate"] == pytest.approx(1 / 3)
    assert stats["p50_latency_ms"] is not None
    assert stats["p95_latency_ms"] is not None


def test_ban_unban_logic() -> None:
    rpc = RPCPool(["https://a.example", "https://b.example"])
    url = "https://a.example"
    for _ in range(12):
        rpc.record_health_for_test(url, ok=False, latency_ms=3000.0, reason="timeout")
    now = time.time()
    assert rpc.is_banned(url, now_s=now + 1.0)
    assert not rpc.is_banned(url, now_s=now + 120.0)


@pytest.mark.asyncio
async def test_sticky_selection_prefers_pinned() -> None:
    rpc = RPCPool(["https://a.example", "https://b.example"])
    rpc.record_health_for_test("https://a.example", ok=False, latency_ms=1200.0, reason="timeout")
    rpc.record_health_for_test("https://b.example", ok=True, latency_ms=120.0, reason="ok")
    pinned = rpc.pick_pinned_endpoint()
    assert pinned == "https://b.example"
    pinned_idx = rpc._resolve_idx(pinned)
    assert pinned_idx is not None
    idx = await rpc._pick_idx(set(), pinned_idx=pinned_idx)
    assert idx == pinned_idx
    await rpc._done_idx(idx, ok=True)
