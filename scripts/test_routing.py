"""
Step 3: BrokerRouter routing test (no live keys required).
Uses a minimal stub broker so we don't depend on Alpaca/Hyperliquid SDKs.
"""
from __future__ import annotations
import sys

from live_trading.broker_protocol import BrokerProtocol, BrokerRouter


class StubBroker:
    def __init__(self, name: str, asset_classes: list[str]) -> None:
        self.name = name
        self.asset_classes = asset_classes

    def get_account(self) -> dict:       return {"broker": self.name, "equity": 0.0}
    def get_positions(self) -> list:     return []
    def submit_order(self, *a, **k) -> dict: return {"order_id": "stub", "status": "accepted"}
    def close_position(self, ticker):    return {"order_id": "stub", "status": "accepted"}
    def get_candles(self, *a, **k):      return []


def main() -> int:
    equity = StubBroker("alpaca-stub", ["equity"])
    perp   = StubBroker("hl-stub",     ["perp", "hip3"])

    # Protocol conformance (structural)
    assert isinstance(equity, BrokerProtocol), "equity stub must satisfy BrokerProtocol"
    assert isinstance(perp,   BrokerProtocol), "perp stub must satisfy BrokerProtocol"
    print("  OK   both stubs conform to BrokerProtocol")

    router = BrokerRouter()
    router.register(equity)
    router.register(perp)

    cases = {
        "AAPL":     ("equity", "alpaca-stub"),
        "MSFT":     ("equity", "alpaca-stub"),
        "BTC":      ("perp",   "hl-stub"),
        "ETH":      ("perp",   "hl-stub"),
        "xyz:TSLA": ("hip3",   "hl-stub"),
    }
    for ticker, (klass, expected_name) in cases.items():
        got_class = router.classify_ticker(ticker)
        got_broker = router.for_ticker(ticker)
        assert got_class == klass, f"{ticker}: class {got_class} != {klass}"
        assert got_broker is not None and got_broker.name == expected_name, \
            f"{ticker}: broker {got_broker} != {expected_name}"
        print(f"  OK   {ticker:10s} -> {got_class:6s} -> {got_broker.name}")

    print("\n[test_routing] All routing checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"[test_routing] FAIL: {e}")
        sys.exit(1)
