import json
from pathlib import Path
from typing import Any, Dict

from mempool.engine import MempoolEngine, TriggerRecord
from mempool.types import DecodedSwap, Trigger


class DummyRPC:
    async def call(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("dummy rpc")

    def stats(self) -> list:
        return []


def _make_engine(tmp_path: Path) -> MempoolEngine:
    return MempoolEngine(
        rpc=DummyRPC(),
        ws_urls=[],
        filter_to=[],
        watch_mode="strict",
        watched_router_set="core",
        min_value_usd=0.0,
        usd_per_eth=0.0,
        max_inflight=1,
        fetch_concurrency=1,
        dedup_ttl_s=1,
        trigger_budget_s=0.1,
        trigger_prepare_budget_ms=50,
        trigger_queue_max=1,
        trigger_concurrency=1,
        trigger_ttl_s=1,
        confirm_timeout_s=0.1,
        post_scan_budget_s=0.1,
        log_dir=tmp_path,
        ui_push=None,
    )


def test_decoded_amounts_are_strings(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    decoded = DecodedSwap(
        tx_hash="0xdead",
        kind="v2_swap",
        router="0xrouter",
        token_in="0x0000000000000000000000000000000000000001",
        token_out="0x0000000000000000000000000000000000000002",
        amount_in=10**24,
        amount_out_min=10**23,
        path=None,
        fee_tiers=None,
        recipient=None,
        deadline=None,
        seen_at_ms=0,
    )
    summary = engine._decoded_summary(decoded)
    assert isinstance(summary["amount_in"], str)
    assert isinstance(summary["amount_out_min"], str)
    assert "e" not in summary["amount_in"].lower()
    assert "e" not in summary["amount_out_min"].lower()


def test_trigger_amount_in_logged_as_string(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path)
    trigger = Trigger(
        trigger_id="trg-1",
        tx_hash="0xabc",
        trigger_type="backrun_candidate",
        tokens_involved=["0x0000000000000000000000000000000000000001"],
        token_universe=["0x0000000000000000000000000000000000000001"],
        suggested_routes=None,
        created_at_ms=0,
        amount_in=10**24,
        token_in="0x0000000000000000000000000000000000000001",
    )
    record = TriggerRecord(
        trigger=trigger,
        pre_result={"best_gross": 0.0, "best_net": 0.0},
        created_at_ms=0,
        pre_block=None,
        post_block=None,
        post_best_net=None,
    )
    engine._append_trigger_result(record, post_result=None)
    data = json.loads(engine.trigger_log.read_text(encoding="utf-8").strip())
    assert isinstance(data.get("trigger_amount_in"), str)
    assert "e" not in data.get("trigger_amount_in", "").lower()
