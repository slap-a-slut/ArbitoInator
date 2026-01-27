# bot/config.py
# NOTE:
# Do not hardcode private keys in the repo. If/when you move to execution mode,
# provide a key via env var (e.g. PRIVATE_KEY) and keep it out of git.

import time
from bot.chain_config import load_chain_config

# Primary RPC (kept for backwards compatibility)
RPC_URL = "https://ethereum-rpc.publicnode.com"
CHAIN_ID = 1

# Optional pool (used when RPC_URLS is provided via config/env/UI).
# Priority order: primary -> secondary -> fallback -> fallback-only.
RPC_URLS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://rpc.ankr.com/eth",
    "https://go.getblock.us",
    "https://eth.merkle.io",
    "https://rpc.flashbots.net",
]
RPC_HTTP_URLS = list(RPC_URLS)

# Priority weights: higher == more preferred (more share).
# publicnode: 3, getblock: 2, merkle: 1, flashbots: 1 (fallback).
RPC_PRIORITY_WEIGHTS = [3.0, 2.0, 1.0, 1.0]

# Endpoints that should be used only when others are saturated/unavailable.
RPC_FALLBACK_ONLY = ["rpc.flashbots.net"]

# RPC timeouts (seconds). All RPC calls are clamped to this range.
RPC_TIMEOUT_MIN_S = 2.0
RPC_TIMEOUT_MAX_S = 4.0
RPC_DEFAULT_TIMEOUT_S = 3.0
RPC_TIMEOUT_S = 3.0
RPC_RETRY_COUNT = 1
RPC_BACKOFF_BASE_S = 0.35
RPC_RATE_LIMIT_BACKOFF_S = 0.35

# Batch eth_call (quotes/pool state)
RPC_BATCH_ETH_CALLS = True
RPC_BATCH_MAX_CALLS = 80
RPC_BATCH_FLUSH_MS = 8

# RPC health tracking / auto-ban
RPC_HEALTH_WINDOW = 50
RPC_BAN_SECONDS = 60
RPC_HEALTH_BAN_SECONDS = 60
RPC_BAN_TIMEOUT_RATE = 0.2
RPC_TIMEOUT_RATE_THRESHOLD = 0.2
RPC_BAN_SUCCESS_RATE = 0.7
RPC_BAN_LATENCY_P95_MS = 2500.0
RPC_LATENCY_P95_MS_THRESHOLD = 2500.0
RPC_OUT_OF_SYNC_BAN_SECONDS = 60

# Circuit breaker: N consecutive errors -> open for cooldown.
RPC_CB_THRESHOLD = 5
RPC_CB_COOLDOWN_S = 30.0

# Mempool settings (public WS monitoring, simulated only)
MEMPOOL_ENABLED = False
MEMPOOL_WS_URLS = [
    "wss://ethereum.publicnode.com",
    "wss://eth.llamarpc.com/ws",
]
RPC_WS_URLS = list(MEMPOOL_WS_URLS)
MEMPOOL_MAX_INFLIGHT_TX = 200
MEMPOOL_FETCH_TX_CONCURRENCY = 20
TX_FETCH_PER_ENDPOINT_MAX_INFLIGHT = 4
TX_FETCH_BATCH_ENABLED = True
TX_FETCH_MAX_RETRIES = 3
TX_FETCH_RETRY_BACKOFF_MS = [200, 500, 1000]
TX_FETCH_BATCH_DISABLE_S = 300
MEMPOOL_MIN_VALUE_USD = 25.0
MEMPOOL_USD_PER_ETH = 2000.0
MEMPOOL_DEDUP_TTL_S = 120
MEMPOOL_TRIGGER_SCAN_BUDGET_S = 1.5
# Trigger scan staging budgets
TRIGGER_PREPARE_BUDGET_MS = 250
TRIGGER_PROBE_BUDGET_MS = 0
TRIGGER_REFINE_BUDGET_MS = 0
TRIGGER_PROBE_BUDGET_RATIO = 0.4
TRIGGER_PROBE_TOP_K = 12
TRIGGER_PROBE_GAS_UNITS = 180_000
TRIGGER_PROBE_MIN_NET = 0.0
TRIGGER_PROBE_AMOUNTS_USDC = [50.0, 100.0]
TRIGGER_PROBE_AMOUNTS_WETH = [0.005, 0.01]
MEMPOOL_TRIGGER_MAX_QUEUE = 50
MEMPOOL_TRIGGER_MAX_CONCURRENT = 1
MEMPOOL_TRIGGER_TTL_S = 60
MEMPOOL_CONFIRM_TIMEOUT_S = 2.0
MEMPOOL_POST_SCAN_BUDGET_S = 1.0
MEMPOOL_TRIGGER_MIN_STABLE = 1000.0
MEMPOOL_TRIGGER_MIN_WETH = 0.5
MEMPOOL_TRIGGER_MIN_UNKNOWN = None  # set float to allow unknown tokens
MEMPOOL_TRIGGER_MIN_BY_TOKEN = {}
MEMPOOL_ALLOW_UNKNOWN_TOKENS = True
MEMPOOL_RAW_MIN_ENABLED = False
MEMPOOL_STRICT_UNKNOWN_TOKENS = False

# Per-block caches (quotes/edges). TTL is one block (cleared on prepare_block).
QUOTE_CACHE_ENABLED = True
VIABILITY_CACHE_ENABLED = True
POOL_DISCOVERY_TTL_BLOCKS = 300

# If a block returns too many failed/invalid quotes, treat it as out-of-sync
# and retry N-1 (prevents mixing blocks across RPCs).
RPC_OUT_OF_SYNC_FAIL_RATIO = 0.6
RPC_OUT_OF_SYNC_MIN_CANDIDATES = 40

# V2-style pool filters
V2_MIN_RESERVE_RATIO = 20.0  # reserve_in must be >= amount_in * ratio
V2_MAX_PRICE_IMPACT_BPS = 300  # 3.00%

# Profit safety buffer (applied in fork_test thresholds)
MEV_BUFFER_BPS = 5.0  # 0.05%

TOKENS = {
    # Majors
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",

    # Stables
    "USDC": "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",

    # Liquid alts
    "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
    "UNI":  "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    "AAVE": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DdAE9",
    "LDO":  "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",
    "MKR":  "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2",
    "COMP": "0xc00e94Cb662C3520282E6f5717214004A7f26888",
    "SUSHI": "0x6B3595068778DD592e39A122f4f5a5CF09C90fE2",
    "CRV":  "0xD533a949740bb3306d119CC777fa900bA034cd52",
    "SNX":  "0xC011A72400E58ecD99Ee497CF89E3775d4bd732F",
    "BAL":  "0xba100000625a3754423978a60c9317c58a424e3D",
    "MATIC": "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0",
}

# Fast decimals lookup (avoid chain calls)
TOKEN_DECIMALS = {
    "WETH": 18,
    "WBTC": 8,
    "USDC": 6,
    "USDT": 6,
    "DAI": 18,
    "LINK": 18,
    "UNI": 18,
    "AAVE": 18,
    "LDO": 18,
    "MKR": 18,
    "COMP": 18,
    "SUSHI": 18,
    "CRV": 18,
    "SNX": 18,
    "BAL": 18,
    "MATIC": 18,
}

# Strategy defaults (symbols). Override in user config if needed.
STRATEGY_BASES = ["USDC", "USDT", "DAI"]
STRATEGY_UNIVERSE = ["WETH", "WBTC", "LINK", "UNI", "AAVE", "LDO", "MKR", "COMP", "SUSHI", "CRV", "SNX", "BAL", "MATIC"]
STRATEGY_HUBS = ["WETH", "USDC", "USDT", "DAI"]
STRATEGY_MAX_HOPS = 3
STRATEGY_MAX_MIDS = 8
BEAM_MAX_DRAWDOWN = 0.35

# Fast reverse lookup (address -> symbol). Lower-case for stable comparisons.
TOKEN_BY_ADDR = {str(addr).lower(): sym for sym, addr in TOKENS.items() if addr}
TOKEN_METADATA = {}  # addr -> {symbol, decimals, updated_at}
TOKEN_METADATA_FAILED_AT = {}  # addr -> timestamp
TOKEN_METADATA_RETRY_S = 600


def _norm_token(token: str) -> str:
    return str(token).strip()


def token_address(token: str) -> str:
    """Return canonical address if token is a known symbol, else return input."""
    t = _norm_token(token)
    if not t:
        return t
    if t in TOKENS and TOKENS[t]:
        return TOKENS[t]
    t_up = t.upper()
    if t_up in TOKENS and TOKENS[t_up]:
        return TOKENS[t_up]
    return t


def token_symbol(token: str) -> str:
    """Return symbol for known token, or a short address string."""
    t = _norm_token(token)
    if not t:
        return ""
    if t in TOKENS and TOKENS[t]:
        return t
    t_up = t.upper()
    if t_up in TOKENS and TOKENS[t_up]:
        return t_up
    sym = TOKEN_BY_ADDR.get(t.lower())
    if sym:
        return sym
    if t.startswith("0x") and len(t) > 10:
        return t[:6] + "..." + t[-4:]
    return t


def token_decimals(token: str) -> int:
    """Return decimals for known token symbol/address."""
    t = _norm_token(token)
    if not t:
        return 18
    if t in TOKEN_DECIMALS:
        return int(TOKEN_DECIMALS[t])
    t_up = t.upper()
    if t_up in TOKEN_DECIMALS:
        return int(TOKEN_DECIMALS[t_up])
    sym = TOKEN_BY_ADDR.get(t.lower())
    if sym:
        return int(TOKEN_DECIMALS.get(sym, 18))
    return 18


def is_known_token(token: str) -> bool:
    addr = _norm_token(token).lower()
    return bool(addr) and addr in TOKEN_BY_ADDR


def register_token_metadata(addr: str, symbol: str, decimals: int) -> None:
    a = str(addr or "").lower()
    if not a:
        return
    sym = str(symbol or "").strip().upper() if symbol else ""
    if not sym:
        sym = token_symbol(a) or (a[:6] + "..." + a[-4:])
    try:
        dec = int(decimals)
    except Exception:
        dec = 18
    TOKEN_METADATA[a] = {"symbol": sym, "decimals": dec, "updated_at": int(time.time())}
    TOKEN_BY_ADDR[a] = sym
    if sym not in TOKENS or not TOKENS.get(sym):
        TOKENS[sym] = addr
    TOKEN_DECIMALS[sym] = dec
    TOKEN_DECIMALS[a] = dec
    TOKEN_METADATA_FAILED_AT.pop(a, None)


def mark_token_metadata_failed(addr: str) -> None:
    a = str(addr or "").lower()
    if not a:
        return
    TOKEN_METADATA_FAILED_AT[a] = int(time.time())

# Uniswap V3 Quoter — mainnet
UNISWAP_V3_QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

# Uniswap V3 QuoterV2 — mainnet (supports gasEstimate in the quote)
# Source: Uniswap v3 Ethereum deployments list.
UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# Uniswap V3 Factory — mainnet
UNISWAP_V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

# Fee tiers (в bps)
FEE_TIERS = [100, 500, 3000, 10000]  # 0.01%, 0.05%, 0.3%, 1%
POOL_DISCOVERY_FEE_TIERS = [500, 3000, 10000]

# Supported DEX adapters (names used in UI/config).
DEXES = ["univ3"]

# Uniswap V2-style factories (mainnet).
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
SUSHISWAP_FACTORY = "0xC0AEe478e3658e2610c5F7A4A2E1777Ce9e4f2Ac"

# Uniswap V2-style routers (mainnet).
UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
SUSHISWAP_ROUTER = "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"

# Uniswap V3 swap routers (mainnet).
UNISWAP_V3_SWAP_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
UNISWAP_V3_SWAP_ROUTER02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"

# Uniswap Universal Router (mainnet).
UNISWAP_UNIVERSAL_ROUTER = "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B"

# Mempool router filter default.
MEMPOOL_FILTER_TO = [
    x for x in (
        UNISWAP_V2_ROUTER,
        SUSHISWAP_ROUTER,
        UNISWAP_V3_SWAP_ROUTER,
        UNISWAP_V3_SWAP_ROUTER02,
        UNISWAP_UNIVERSAL_ROUTER,
    )
    if x
]
MEMPOOL_WATCH_MODE = "strict"  # strict | routers_only
MEMPOOL_WATCHED_ROUTER_SETS = "core"  # core | extended

MEMPOOL_STABLE_SET = [
    TOKENS.get("USDC"),
    TOKENS.get("USDT"),
    TOKENS.get("DAI"),
]
MEMPOOL_WETH = TOKENS.get("WETH")
MEMPOOL_TRIGGER_CONNECTORS = [
    TOKENS.get("WETH"),
    TOKENS.get("USDC"),
    TOKENS.get("USDT"),
    TOKENS.get("DAI"),
]
MEMPOOL_TRIGGER_EXTRA_TOKENS = []
MEMPOOL_TRIGGER_BASE_FALLBACK = [
    TOKENS.get("USDC"),
    TOKENS.get("USDT"),
    TOKENS.get("DAI"),
]

# Trigger scan cross-DEX preferences
TRIGGER_REQUIRE_CROSS_DEX = True
TRIGGER_PREFER_CROSS_DEX = True
TRIGGER_REQUIRE_THREE_HOPS = True
TRIGGER_CROSS_DEX_BONUS_BPS = 5.0
TRIGGER_SAME_DEX_PENALTY_BPS = 5.0
TRIGGER_EDGE_TOP_M_PER_DEX = 2
TRIGGER_BASE_FALLBACK_ENABLED = True
TRIGGER_ALLOW_TWO_HOP_FALLBACK = True
TRIGGER_CROSS_DEX_FALLBACK = True
TRIGGER_MAX_CANDIDATES_RAW = 80

# Execution simulation
EXEC_SIM_BACKENDS = ["trace", "state_override", "quote"]
ARB_EXECUTOR_ADDRESS = ""
ARB_EXECUTOR_OWNER = ""
SIM_FROM_ADDRESS = "0x0000000000000000000000000000000000000000"
EXECUTION_MODE = "off"  # off | dryrun
PREFLIGHT_ENFORCE_ALLOWANCE = False

# Uniswap V2-style fees in bps (0.30% == 30 bps).
UNISWAP_V2_FEE_BPS = 30
SUSHISWAP_FEE_BPS = 30

# Fallback gas estimates for quoting when DEX doesn't provide gas.
V3_GAS_ESTIMATE = 110_000
V2_GAS_ESTIMATE = 90_000


def _refresh_token_maps() -> None:
    global TOKEN_BY_ADDR, MEMPOOL_STABLE_SET, MEMPOOL_WETH, MEMPOOL_TRIGGER_CONNECTORS, MEMPOOL_TRIGGER_BASE_FALLBACK
    TOKEN_BY_ADDR = {str(addr).lower(): sym for sym, addr in TOKENS.items() if addr}
    MEMPOOL_STABLE_SET = [TOKENS.get("USDC"), TOKENS.get("USDT"), TOKENS.get("DAI")]
    MEMPOOL_WETH = TOKENS.get("WETH")
    MEMPOOL_TRIGGER_CONNECTORS = [TOKENS.get("WETH"), TOKENS.get("USDC"), TOKENS.get("USDT"), TOKENS.get("DAI")]
    MEMPOOL_TRIGGER_BASE_FALLBACK = [TOKENS.get("USDC"), TOKENS.get("USDT"), TOKENS.get("DAI")]


def _apply_chain_overrides() -> None:
    chain_cfg = load_chain_config()
    if not chain_cfg:
        return

    global RPC_URLS, RPC_URL, CHAIN_ID, RPC_HTTP_URLS
    if chain_cfg.rpc_urls:
        RPC_URLS = list(chain_cfg.rpc_urls)
        RPC_HTTP_URLS = list(RPC_URLS)
        RPC_URL = RPC_URLS[0]
    if chain_cfg.chain_id is not None:
        CHAIN_ID = int(chain_cfg.chain_id)

    if chain_cfg.tokens:
        for sym, addr in chain_cfg.tokens.items():
            TOKENS[str(sym).upper()] = addr
    if chain_cfg.token_decimals:
        for sym, dec in chain_cfg.token_decimals.items():
            try:
                TOKEN_DECIMALS[str(sym).upper()] = int(dec)
            except Exception:
                continue

    dex = chain_cfg.dex or {}

    def _set_if_present(key: str, target_name: str) -> None:
        if key in dex:
            globals()[target_name] = dex.get(key, "")

    _set_if_present("uniswap_v3_quoter", "UNISWAP_V3_QUOTER")
    _set_if_present("uniswap_v3_quoter_v2", "UNISWAP_V3_QUOTER_V2")
    _set_if_present("uniswap_v3_factory", "UNISWAP_V3_FACTORY")
    _set_if_present("uniswap_v2_factory", "UNISWAP_V2_FACTORY")
    _set_if_present("sushiswap_factory", "SUSHISWAP_FACTORY")
    _set_if_present("uniswap_v2_router", "UNISWAP_V2_ROUTER")
    _set_if_present("sushiswap_router", "SUSHISWAP_ROUTER")
    _set_if_present("uniswap_v3_swap_router", "UNISWAP_V3_SWAP_ROUTER")
    _set_if_present("uniswap_v3_swap_router02", "UNISWAP_V3_SWAP_ROUTER02")
    _set_if_present("uniswap_universal_router", "UNISWAP_UNIVERSAL_ROUTER")
    _set_if_present("arb_executor_address", "ARB_EXECUTOR_ADDRESS")

    global MEMPOOL_FILTER_TO
    MEMPOOL_FILTER_TO = [
        x for x in (
            UNISWAP_V2_ROUTER,
            SUSHISWAP_ROUTER,
            UNISWAP_V3_SWAP_ROUTER,
            UNISWAP_V3_SWAP_ROUTER02,
            UNISWAP_UNIVERSAL_ROUTER,
        )
        if x
    ]

    _refresh_token_maps()


_apply_chain_overrides()
