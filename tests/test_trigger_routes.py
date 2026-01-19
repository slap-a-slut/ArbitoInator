from fork_test import _build_trigger_routes


def test_trigger_routes_require_three_hops() -> None:
    tokens = [
        "0x0000000000000000000000000000000000000001",
        "0x0000000000000000000000000000000000000002",
    ]
    routes = _build_trigger_routes(tokens, max_hops=3, token_universe=tokens, require_three_hops=True)
    assert routes
    assert all(len(r) == 4 for r in routes)
