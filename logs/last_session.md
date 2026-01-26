# Last Session Summary

- Started (UTC): 2026-01-26T05:11:56
- Updated (UTC): 2026-01-26T05:12:11
- Duration: 00:00:15
- RPC endpoints: 3
- DEX adapters: 3
- Blocks scanned: 1
- Profit hits: 0

## RPC URLs
- https://ethereum-rpc.publicnode.com
- https://eth.llamarpc.com
- https://rpc.ankr.com/eth

## DEX adapters
- univ3
- univ2
- sushiswap

## Runtime flags
- SIM_PROFILE: 
- DEBUG_FUNNEL: 1
- GAS_OFF: 1
- FIXED_GAS_UNITS: 
- ENABLE_MULTIDEX: 1
- SCAN_SOURCE: hybrid
- MEMPOOL_ENABLED: 1

## Settings
```yaml
amount_presets: (10.0, 50.0, 100.0)
arb_executor_address: 
arb_executor_owner: 
beam_k: 12
block_budget_s: 30.0
concurrency: 12
dexes: ('univ3', 'univ2', 'sushiswap')
edge_top_m: 2
enable_multidex: True
execution_mode: off
expand_budget_max_s: 6.0
expand_ratio_cap: 0.6
max_candidates_stage1: 200
max_expanded_per_candidate: 6
max_gas_gwei: None
max_hops: 3
max_total_expanded: 400
mempool_allow_unknown_tokens: True
mempool_confirm_timeout_s: 2.0
mempool_dedup_ttl_s: 120
mempool_enabled: True
mempool_fetch_tx_concurrency: 15
mempool_filter_to: ()
mempool_max_inflight_tx: 200
mempool_min_value_usd: 50.0
mempool_post_scan_budget_s: 1.0
mempool_raw_min_enabled: False
mempool_strict_unknown_tokens: False
mempool_trigger_max_concurrent: 1
mempool_trigger_max_queue: 50
mempool_trigger_scan_budget_s: 2.5
mempool_trigger_ttl_s: 60
mempool_usd_per_eth: 2000.0
mempool_watch_mode: strict
mempool_watched_router_sets: core
mempool_ws_urls: ('wss://ethereum.publicnode.com', 'wss://eth.llamarpc.com/ws')
mev_buffer_bps: 0.0
min_first_task_s: 0.08
min_profit_abs: 0
min_profit_pct: 0
min_scan_reserve_s: 0.6
out_of_sync_ban_seconds: 60
prepare_budget_max_s: 6.0
prepare_budget_min_s: 2.0
prepare_budget_ratio: 0.2
probe_amount: 1.0
report_currency: USDC
rpc_health_ban_seconds: 60
rpc_http_endpoints: ({'id': 'publicnode', 'url': 'https://ethereum-rpc.publicnode.com'}, {'id': 'llama', 'url': 'https://eth.llamarpc.com'}, {'id': 'ankr', 'url': 'https://rpc.ankr.com/eth'})
rpc_latency_p95_ms_threshold: 2500.0
rpc_retry_count: 1
rpc_timeout_rate_threshold: 0.2
rpc_timeout_s: 3.0
rpc_timeout_stage1_s: 4
rpc_timeout_stage2_s: 6
rpc_urls: ('https://ethereum-rpc.publicnode.com', 'https://eth.llamarpc.com', 'https://rpc.ankr.com/eth')
rpc_ws_endpoints: []
rpc_ws_pairing: {}
scan_mode: auto
scan_source: hybrid
sim_backend: quote
slippage_bps: 0
stage1_amount: 1
stage1_fee_tiers: (500, 3000)
stage2_amount_max: 8000
stage2_amount_min: 1000
stage2_max_evals: 16
stage2_top_k: 120
trigger_allow_two_hop_fallback: True
trigger_base_fallback_enabled: True
trigger_connectors: ('WETH', 'USDC', 'USDT', 'DAI')
trigger_cross_dex_bonus_bps: 5.0
trigger_cross_dex_fallback: True
trigger_edge_top_m_per_dex: 2
trigger_max_candidates_raw: 80
trigger_prefer_cross_dex: True
trigger_prepare_budget_ms: 250
trigger_require_cross_dex: False
trigger_require_three_hops: False
trigger_same_dex_penalty_bps: 5.0
tx_fetch_batch_enabled: True
tx_fetch_max_retries: 3
tx_fetch_per_endpoint_max_inflight: 4
tx_fetch_retry_backoff_ms: (200, 500, 1000)
v2_max_price_impact_bps: 0.0
v2_min_reserve_ratio: 0.0
```
