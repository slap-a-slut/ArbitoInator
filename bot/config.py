# bot/config.py

RPC_URL = "https://eth.llamarpc.com"

TOKENS = {
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "USDC": "0xA0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
}

# Uniswap V3 Quoter — mainnet
UNISWAP_V3_QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"

# Fee tiers (в bps)
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.3%, 1%
