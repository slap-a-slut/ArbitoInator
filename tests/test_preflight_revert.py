from eth_abi import encode as abi_encode

from bot.preflight import decode_revert_reason


def test_decode_revert_reason_error() -> None:
    data = "0x08c379a0" + abi_encode(["string"], ["boom"]).hex()
    assert decode_revert_reason(data) == "revert:boom"


def test_decode_revert_reason_panic() -> None:
    data = "0x4e487b71" + abi_encode(["uint256"], [0x11]).hex()
    assert decode_revert_reason(data) == "panic:0x11"
