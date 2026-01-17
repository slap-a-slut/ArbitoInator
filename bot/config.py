# bot/config.py
# NOTE:
# Do not hardcode private keys in the repo. If/when you move to execution mode,
# provide a key via env var (e.g. PRIVATE_KEY) and keep it out of git.

# Primary RPC (kept for backwards compatibility)
RPC_URL = "https://rpc.flashbots.net"

# Optional pool (used when RPC_URLS is provided via config/env/UI).
# Keep a few free endpoints for fallback; users can override in bot_config.json.
RPC_URLS = [
    "https://rpc.flashbots.net",
    "https://cloudflare-eth.com",
    "https://eth.llamarpc.com",
]

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
}

# Strategy defaults (symbols). Override in user config if needed.
STRATEGY_BASES = ["USDC", "USDT", "DAI"]
STRATEGY_UNIVERSE = ["WETH", "WBTC", "LINK", "UNI", "AAVE", "LDO"]
STRATEGY_HUBS = ["WETH", "USDC", "USDT", "DAI"]

# Fast reverse lookup (address -> symbol). Lower-case for stable comparisons.
TOKEN_BY_ADDR = {str(addr).lower(): sym for sym, addr in TOKENS.items()}


def _norm_token(token: str) -> str:
    return str(token).strip()


def token_address(token: str) -> str:
    """Return canonical address if token is a known symbol, else return input."""
    t = _norm_token(token)
    if not t:
        return t
    if t in TOKENS:
        return TOKENS[t]
    t_up = t.upper()
    if t_up in TOKENS:
        return TOKENS[t_up]
    return t


def token_symbol(token: str) -> str:
    """Return symbol for known token, or a short address string."""
    t = _norm_token(token)
    if not t:
        return ""
    if t in TOKENS:
        return t
    t_up = t.upper()
    if t_up in TOKENS:
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

# Uniswap V3 Quoter — mainnet
UNISWAP_V3_QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

# Uniswap V3 QuoterV2 — mainnet (supports gasEstimate in the quote)
# Source: Uniswap v3 Ethereum deployments list.
UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# Fee tiers (в bps)
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.3%, 1%

# Supported DEX adapters (names used in UI/config).
DEXES = ["univ3", "univ2", "sushiswap"]

# Uniswap V2-style factories (mainnet).
UNISWAP_V2_FACTORY = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
SUSHISWAP_FACTORY = "0xC0AEe478e3658e2610c5F7A4A2E1777Ce9e4f2Ac"

# Uniswap V2-style fees in bps (0.30% == 30 bps).
UNISWAP_V2_FEE_BPS = 30
SUSHISWAP_FEE_BPS = 30
