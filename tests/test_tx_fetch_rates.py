from mempool.tx_fetcher import TxFetcher


class DummyRPC:
    def __init__(self) -> None:
        self.urls = ["https://example.com"]


def test_tx_fetch_rate_invariants() -> None:
    rpc = DummyRPC()
    fetcher = TxFetcher(rpc, http_urls=["https://example.com"], ws_urls=[])

    fetcher._note_attempts(3)
    fetcher._note_attempt_found(2)
    fetcher._note_unique_total("0xaaa")
    fetcher._note_unique_total("0xbbb")
    fetcher._note_unique_found("0xaaa")
    fetcher._note_unique_found("0xaaa")

    status = fetcher.status()
    assert status["tx_fetch_attempts_found"] <= status["tx_fetch_attempts_total"]
    assert status["tx_fetch_unique_found"] <= status["tx_fetch_unique_total"]
