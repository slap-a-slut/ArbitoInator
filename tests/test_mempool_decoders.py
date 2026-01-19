import time
from eth_abi import encode

from mempool.decoders.uniswap_v2 import UniswapV2Decoder, SIG_SWAP_EXACT_TOKENS
from mempool.decoders.uniswap_v3 import UniswapV3Decoder, SIG_EXACT_INPUT_SINGLE
from mempool.decoders.universal_router import UniversalRouterDecoder, SIG_EXECUTE
from mempool.decoders.utils import selector
from mempool.types import PendingTx


def _addr(ch: str) -> str:
    return "0x" + (ch * 40)


def _build_input(signature: str, types, values) -> str:
    return "0x" + selector(signature) + encode(types, values).hex()


def test_univ2_decode_exact_tokens() -> None:
    router = _addr("a")
    token_in = _addr("1")
    token_out = _addr("2")
    recipient = _addr("3")
    amount_in = 1_000_000
    amount_out_min = 900_000
    deadline = 123456
    payload = _build_input(
        SIG_SWAP_EXACT_TOKENS,
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, amount_out_min, [token_in, token_out], recipient, deadline],
    )
    tx = PendingTx(
        tx_hash="0xabc",
        from_addr=_addr("f"),
        to_addr=router,
        input=payload,
        value=0,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        tx_type=None,
        nonce=None,
        seen_at_ms=int(time.time() * 1000),
    )
    dec = UniswapV2Decoder({router.lower()})
    out = dec.decode(tx)
    assert out is not None
    assert out.kind == "v2_swap"
    assert out.token_in == token_in.lower()
    assert out.token_out == token_out.lower()
    assert out.amount_in == amount_in
    assert out.amount_out_min == amount_out_min


def test_univ3_decode_exact_input_single() -> None:
    router = _addr("b")
    token_in = _addr("4")
    token_out = _addr("5")
    recipient = _addr("6")
    fee = 3000
    deadline = 555
    amount_in = 2_000_000
    amount_out_min = 1_900_000
    params = (token_in, token_out, fee, recipient, deadline, amount_in, amount_out_min, 0)
    payload = _build_input(
        SIG_EXACT_INPUT_SINGLE,
        ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"],
        [params],
    )
    tx = PendingTx(
        tx_hash="0xdef",
        from_addr=_addr("f"),
        to_addr=router,
        input=payload,
        value=0,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        tx_type=None,
        nonce=None,
        seen_at_ms=int(time.time() * 1000),
    )
    dec = UniswapV3Decoder({router.lower()})
    out = dec.decode(tx)
    assert out is not None
    assert out.kind == "v3_swap"
    assert out.token_in == token_in.lower()
    assert out.token_out == token_out.lower()
    assert out.amount_in == amount_in
    assert out.amount_out_min == amount_out_min
    assert out.fee_tiers == [fee]


def test_universal_router_decode() -> None:
    router = _addr("c")
    payload = _build_input(
        SIG_EXECUTE,
        ["bytes", "bytes[]", "uint256"],
        [b"\x0b", [b"\x01\x02"], 999],
    )
    tx = PendingTx(
        tx_hash="0x999",
        from_addr=_addr("f"),
        to_addr=router,
        input=payload,
        value=0,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        tx_type=None,
        nonce=None,
        seen_at_ms=int(time.time() * 1000),
    )
    dec = UniversalRouterDecoder({router.lower()})
    out = dec.decode(tx)
    assert out is not None
    assert out.kind == "universal_router"
    assert out.router == router.lower()
