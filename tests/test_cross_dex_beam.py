import pytest

from bot.dex.base import QuoteEdge
from fork_test import _beam_candidates_for_route, _make_block_context
import fork_test


class DummyPS:
    def __init__(self, edges_by_pair):
        self.edges_by_pair = edges_by_pair

    async def quote_edges(self, token_in, token_out, amount_in, *, block_ctx=None, fee_tiers=None, timeout_s=None):
        return self.edges_by_pair.get((token_in, token_out), [])


@pytest.mark.asyncio
async def test_require_cross_dex_filters_same_dex() -> None:
    a = "0x0000000000000000000000000000000000000001"
    b = "0x0000000000000000000000000000000000000002"
    c = "0x0000000000000000000000000000000000000003"
    edges = {
        (a, b): [QuoteEdge("univ3", a, b, 1000, 1100)],
        (b, c): [QuoteEdge("univ3", b, c, 1100, 1200)],
        (c, a): [QuoteEdge("univ3", c, a, 1200, 1005)],
    }
    original = fork_test.PS
    try:
        fork_test.PS = DummyPS(edges)
        block_ctx = _make_block_context(1)
        out = await _beam_candidates_for_route(
            (a, b, c, a),
            amount_in=1000,
            block_ctx=block_ctx,
            fee_tiers=None,
            timeout_s=0.1,
            beam_k=5,
            edge_top_m=2,
            edge_top_m_per_dex=1,
            eval_budget=5,
            require_cross_dex=True,
        )
        assert out == []
    finally:
        fork_test.PS = original
