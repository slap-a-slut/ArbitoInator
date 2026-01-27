import json
from pathlib import Path


PRESET_ALIAS_KEYS = {
    "dex_adapters",
    "enable_multidex_beam",
    "reporting_currency",
    "amounts",
    "slippage_safety_bps",
    "stage2_amount_range",
}

KNOWN_CONFIG_KEYS = {
    "rpc_urls",
    "rpc_http_urls",
    "rpc_http_endpoints",
    "rpc_ws_endpoints",
    "rpc_ws_pairing",
    "dexes",
    "min_profit_pct",
    "min_profit_abs",
    "slippage_bps",
    "mev_buffer_bps",
    "max_gas_gwei",
    "concurrency",
    "block_budget_s",
    "prepare_budget_ratio",
    "prepare_budget_min_s",
    "prepare_budget_max_s",
    "max_candidates_stage1",
    "max_total_expanded",
    "max_expanded_per_candidate",
    "rpc_timeout_s",
    "rpc_retry_count",
    "rpc_batch_eth_calls",
    "rpc_batch_max_calls",
    "rpc_batch_flush_ms",
    "enable_multidex",
    "max_hops",
    "beam_k",
    "edge_top_m",
    "trigger_prefer_cross_dex",
    "trigger_require_cross_dex",
    "trigger_require_three_hops",
    "trigger_base_fallback_enabled",
    "trigger_allow_two_hop_fallback",
    "trigger_cross_dex_fallback",
    "trigger_connectors",
    "trigger_max_candidates_raw",
    "trigger_prepare_budget_ms",
    "trigger_probe_budget_ms",
    "trigger_refine_budget_ms",
    "trigger_probe_budget_ratio",
    "trigger_probe_top_k",
    "trigger_probe_gas_units",
    "trigger_probe_min_net",
    "trigger_probe_amounts_usdc",
    "trigger_probe_amounts_weth",
    "trigger_cross_dex_bonus_bps",
    "trigger_same_dex_penalty_bps",
    "trigger_edge_top_m_per_dex",
    "probe_amount",
    "scan_mode",
    "scan_source",
    "amount_presets",
    "stage1_amount",
    "stage1_fee_tiers",
    "stage2_top_k",
    "stage2_amount_min",
    "stage2_amount_max",
    "stage2_max_evals",
    "rpc_timeout_stage1_s",
    "rpc_timeout_stage2_s",
    "v2_min_reserve_ratio",
    "v2_max_price_impact_bps",
    "sim_profile",
    "debug_funnel",
    "gas_off",
    "fixed_gas_units",
    "sim_backend",
    "execution_mode",
    "arb_executor_address",
    "arb_executor_owner",
    "mempool_enabled",
    "mempool_ws_urls",
    "mempool_max_inflight_tx",
    "mempool_fetch_tx_concurrency",
    "mempool_filter_to",
    "mempool_watch_mode",
    "mempool_watched_router_sets",
    "mempool_allow_unknown_tokens",
    "mempool_strict_unknown_tokens",
    "mempool_raw_min_enabled",
    "mempool_min_value_usd",
    "mempool_usd_per_eth",
    "mempool_dedup_ttl_s",
    "mempool_trigger_scan_budget_s",
    "mempool_trigger_max_queue",
    "mempool_trigger_max_concurrent",
    "mempool_trigger_ttl_s",
    "mempool_confirm_timeout_s",
    "mempool_post_scan_budget_s",
    "tx_fetch_batch_enabled",
    "tx_fetch_max_retries",
    "tx_fetch_retry_backoff_ms",
    "tx_fetch_per_endpoint_max_inflight",
    "rpc_health_ban_seconds",
    "rpc_timeout_rate_threshold",
    "rpc_latency_p95_ms_threshold",
    "out_of_sync_ban_seconds",
    "report_currency",
    "verbose",
}

ALLOWED_KEYS = PRESET_ALIAS_KEYS | KNOWN_CONFIG_KEYS


def test_presets_schema_and_keys() -> None:
    root = Path(__file__).resolve().parents[1]
    presets_dir = root / "presets"
    assert presets_dir.exists(), "presets/ directory missing"
    files = sorted(presets_dir.glob("*.json"))
    assert files, "no preset files found"

    seen_ids = set()
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{path.name} is not a JSON object"
        preset_id = str(data.get("id", "")).strip()
        name = str(data.get("name", "")).strip()
        settings = data.get("settings")
        assert preset_id, f"{path.name} missing id"
        assert name, f"{path.name} missing name"
        assert isinstance(settings, dict), f"{path.name} settings must be object"
        assert preset_id not in seen_ids, f"duplicate preset id {preset_id}"
        seen_ids.add(preset_id)

        unknown_keys = [k for k in settings.keys() if k not in ALLOWED_KEYS]
        assert not unknown_keys, f"{path.name} has unknown keys: {unknown_keys}"


def test_cross_dex_defaults_for_presets() -> None:
    root = Path(__file__).resolve().parents[1]
    presets_dir = root / "presets"
    balanced = json.loads((presets_dir / "balanced.json").read_text(encoding="utf-8"))
    coverage = json.loads((presets_dir / "coverage_heavy.json").read_text(encoding="utf-8"))
    stress = json.loads((presets_dir / "stress.json").read_text(encoding="utf-8"))
    assert balanced["settings"].get("trigger_prefer_cross_dex") is True
    assert balanced["settings"].get("trigger_require_cross_dex") is False
    assert coverage["settings"].get("trigger_prefer_cross_dex") is True
    assert coverage["settings"].get("trigger_require_cross_dex") is False
    assert stress["settings"].get("trigger_prefer_cross_dex") is False
    assert stress["settings"].get("trigger_require_cross_dex") is False
