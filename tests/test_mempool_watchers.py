import json
import time
from pathlib import Path

import pytest
from eth_abi import encode

from bot import config
from mempool.decoders.uniswap_v2 import SIG_SWAP_EXACT_TOKENS
from mempool.decoders.utils import selector
from mempool.engine import MempoolEngine
from mempool.types import PendingTx


class DummyRPC:
    async def call(self, *_args, **_kwargs):
        raise RuntimeError("dummy rpc")

    def stats(self):
        return []


def _build_input(signature: str, types, values) -> str:
    return "0x" + selector(signature) + encode(types, values).hex()


def _pending_tx(to_addr: str, input_data: str) -> PendingTx:
    return PendingTx(
        tx_hash="0x" + "ab" * 32,
        from_addr="0x" + "11" * 20,
        to_addr=to_addr,
        input=input_data,
        value=0,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        tx_type=None,
        nonce=None,
        seen_at_ms=int(time.time() * 1000),
    )


def _make_engine(tmp_path: Path, watch_mode: str = "strict") -> MempoolEngine:
    return MempoolEngine(
        rpc=DummyRPC(),
        ws_urls=[],
        filter_to=[],
        watch_mode=watch_mode,
        watched_router_set="core",
        min_value_usd=0.0,
        usd_per_eth=0.0,
        max_inflight=1,
        fetch_concurrency=1,
        dedup_ttl_s=1,
        trigger_budget_s=0.1,
        trigger_prepare_budget_ms=50,
        trigger_queue_max=10,
        trigger_concurrency=1,
        trigger_ttl_s=1,
        confirm_timeout_s=0.1,
        post_scan_budget_s=0.1,
        log_dir=tmp_path,
        ui_push=None,
    )


def _read_last_jsonl(path: Path):
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])


@pytest.mark.asyncio
async def test_watched_router_decodes(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, watch_mode="routers_only")
    router = config.UNISWAP_V2_ROUTER
    token_in = config.TOKENS.get("USDC")
    token_out = config.TOKENS.get("WETH")
    assert token_in and token_out
    amount_in = int(1500 * (10 ** config.token_decimals(token_in)))
    amount_out_min = 1
    payload = _build_input(
        SIG_SWAP_EXACT_TOKENS,
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, amount_out_min, [token_in, token_out], token_out, 9999999],
    )
    tx = _pending_tx(router, payload)
    await engine._on_pending_tx(tx)
    entry = _read_last_jsonl(engine.mempool_log)
    assert entry
    assert entry["status"] == "decoded"
    assert entry["watched"] is True
    assert entry["router_name"] == "Uniswap V2 Router02"
    assert entry["router_type"] == "univ2"


@pytest.mark.asyncio
async def test_random_address_non_swap_selector_ignored(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, watch_mode="strict")
    tx = _pending_tx("0x" + "22" * 20, "0x12345678")
    await engine._on_pending_tx(tx)
    entry = _read_last_jsonl(engine.mempool_log)
    assert entry
    assert entry["status"] == "ignored"
    assert entry["ignore_stage"] == "selector_not_swap"
    assert entry["reason"] == "ignored_selector"
    assert entry["watched"] is False


@pytest.mark.asyncio
async def test_selector_match_decodes_without_router(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, watch_mode="strict")
    token_in = config.TOKENS.get("USDC")
    token_out = config.TOKENS.get("WETH")
    assert token_in and token_out
    amount_in = int(1500 * (10 ** config.token_decimals(token_in)))
    payload = _build_input(
        SIG_SWAP_EXACT_TOKENS,
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, 1, [token_in, token_out], token_out, 9999999],
    )
    tx = _pending_tx("0x" + "33" * 20, payload)
    await engine._on_pending_tx(tx)
    entry = _read_last_jsonl(engine.mempool_log)
    assert entry
    assert entry["status"] == "decoded"
    assert entry["watched"] is False
    assert entry["router_name"] is None


@pytest.mark.asyncio
async def test_mempool_counters_increment(tmp_path: Path) -> None:
    engine = _make_engine(tmp_path, watch_mode="strict")
    token_in = config.TOKENS.get("USDC")
    token_out = config.TOKENS.get("WETH")
    assert token_in and token_out
    amount_in = int(1500 * (10 ** config.token_decimals(token_in)))
    payload = _build_input(
        SIG_SWAP_EXACT_TOKENS,
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, 1, [token_in, token_out], token_out, 9999999],
    )
    await engine._on_pending_tx(_pending_tx(config.UNISWAP_V2_ROUTER, payload))
    await engine._on_pending_tx(_pending_tx("0x" + "44" * 20, "0x12345678"))
    await engine._on_pending_tx(_pending_tx("0x" + "55" * 20, payload))

    await engine._on_status({})
    status = json.loads((tmp_path / "mempool_status.json").read_text(encoding="utf-8"))
    assert status["mempool_seen_total"] == 3
    assert status["mempool_watched_total"] == 1
    assert status["mempool_decoded_total"] == 2
    assert status["mempool_ignored_selector"] == 1
