# Testing Profiles

This project uses `fork_test.py` for local fork simulations. Settings come from `bot_config.json` (or the UI), and a few env flags override behavior.

## Profiles

### fast-smoke
Goal: quick health check (1–2 minutes) to verify scanner, UI, and logs.

Recommended settings (bot_config.json):
- `concurrency`: 6
- `block_budget_s`: 4
- `stage1_amount`: 200
- `stage2_top_k`: 8
- `rpc_timeout_stage1_s`: 4
- `rpc_timeout_stage2_s`: 6

Command:
```bash
python3 -u fork_test.py
```

Expected:
- Non-zero `candidates_total`
- Some `raw_opps` (gross > 0) per block

### debug-funnel
Goal: understand *where* candidates die (gross/gas/safety/ready).

Recommended settings:
- `SIM_PROFILE=debug` (more permissive thresholds)

Command:
```bash
DEBUG_FUNNEL=1 SIM_PROFILE=debug python3 -u fork_test.py
```

Expected:
- `[Funnel]` logs with top-5 gross/net candidates
- `raw_opps > 0` in most blocks

### gas-off
Goal: verify gross-only opportunities exist without gas drag.

Command:
```bash
GAS_OFF=1 DEBUG_FUNNEL=1 SIM_PROFILE=debug python3 -u fork_test.py
```

Expected:
- `raw_opps` and `gas_opps` should both rise
- `profit_net` should roughly match `profit_gross`

### fixed-gas
Goal: stabilize gas effect to compare across blocks.

Command:
```bash
FIXED_GAS_UNITS=180000 DEBUG_FUNNEL=1 SIM_PROFILE=debug python3 -u fork_test.py
```

Expected:
- More consistent `profit_net` shifts
- Easier to spot sensitivity to gas

### rpc-safe
Goal: reduce out-of-sync errors across multi-RPC setups.

Recommended settings:
- `concurrency`: 6
- `rpc_timeout_stage1_s`: 6
- `rpc_timeout_stage2_s`: 10
- `block_budget_s`: 12

Notes:
- Out-of-sync fallback triggers when the failure ratio exceeds
  `RPC_OUT_OF_SYNC_FAIL_RATIO` (default 0.6) and candidates >=
  `RPC_OUT_OF_SYNC_MIN_CANDIDATES` (default 40).

Command:
```bash
DEBUG_FUNNEL=1 python3 -u fork_test.py
```

## What to expect in logs
- `raw_opps`: gross profit > 0 (no gas)
- `gas_opps`: net profit > 0 (after gas)
- `safety_opps`: net profit minus slippage/MEV buffer > 0
- `final_opps`: passes risk + thresholds

`blocks.jsonl` now includes:
- `profit_gross_raw`, `profit_net_raw`, `gas_cost`
- `scan_*` stats to detect out-of-sync or timeouts

## If everything is still 0
Checklist:
- Ensure RPC endpoints are fully synced and not rate-limited.
- Try `GAS_OFF=1` to confirm gross opportunities exist.
- Use `DEBUG_FUNNEL=1` and inspect reject reasons.
- Increase `block_budget_s` and reduce `concurrency`.
- Verify `report_currency` matches your base routes (USDC/USDT).
