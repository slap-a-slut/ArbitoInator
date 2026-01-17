# bot/config.py
# NOTE:
# Do not hardcode private keys in the repo. If/when you move to execution mode,
# provide a key via env var (e.g. PRIVATE_KEY) and keep it out of git.

# Primary RPC (kept for backwards compatibility)
RPC_URL = "https://rpc.flashbots.net"

# Optional failover list. If env RPC_URLS is set, it takes precedence.
RPC_URLS = [
    "https://rpc.flashbots.net",
    "https://cloudflare-eth.com",
    "https://eth.llamarpc.com",
]

# Optional pool (used when RPC_URLS is provided via config/env/UI)
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

# Uniswap V3 Quoter — mainnet
UNISWAP_V3_QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

# Uniswap V3 QuoterV2 — mainnet (supports gasEstimate in the quote)
# Source: Uniswap v3 Ethereum deployments list.
UNISWAP_V3_QUOTER_V2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"

# Fee tiers (в bps)
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.3%, 1%
