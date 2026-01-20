from eth_utils import function_signature_to_4byte_selector

from bot.arb_builder import build_execute_call_from_payload
from bot import config


def test_executor_calldata_builder_prefix() -> None:
    usdc = config.TOKENS["USDC"]
    weth = config.TOKENS["WETH"]
    payload = {
        "route": [usdc, weth, usdc],
        "hops": [
            {"token_in": usdc, "token_out": weth, "dex_id": "univ2", "params": {}},
            {"token_in": weth, "token_out": usdc, "dex_id": "univ3", "params": {"fee_tier": 3000}},
        ],
        "hop_amounts": [
            {"amount_in": 1_000_000, "amount_out": 900_000},
            {"amount_in": 900_000, "amount_out": 1_050_000},
        ],
    }
    call = build_execute_call_from_payload(
        payload,
        min_profit=1,
        to_addr="0x0000000000000000000000000000000000000001",
        slippage_bps=50,
    )
    assert call and isinstance(call, (bytes, bytearray))
    selector = function_signature_to_4byte_selector(
        "execute((address,address,uint256,uint256,bytes)[],uint256,address,address)"
    )
    assert call[:4] == selector
    assert len(call) > 4
