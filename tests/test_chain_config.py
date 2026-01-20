from bot.chain_config import load_chain_config


def test_load_chain_config_mainnet() -> None:
    cfg = load_chain_config("mainnet")
    assert cfg is not None
    assert cfg.chain_id == 1
    assert cfg.tokens.get("WETH")
    assert cfg.token_decimals.get("USDC") == 6


def test_load_chain_config_sepolia() -> None:
    cfg = load_chain_config("sepolia")
    assert cfg is not None
    assert cfg.chain_id == 11155111
    assert cfg.tokens.get("WETH")
