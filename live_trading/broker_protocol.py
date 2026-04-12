"""
Broker protocol — abstract surface area any broker adapter must implement.

Defines a common interface so StrategyManager / live trading code can route
orders to Alpaca (equities), Hyperliquid (perps + HIP-3 tradfi), or any
future adapter without knowing the underlying SDK.

All return values are plain dicts/lists-of-dicts so they can be JSON-serialized
and logged to the diary or shipped over the FastAPI layer.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Canonical dict shapes (documented for adapter implementers)
# ---------------------------------------------------------------------------
#
# Account:
#   {
#       "equity":         float,  # total portfolio value incl. unrealized PnL
#       "cash":           float,  # withdrawable / settled cash
#       "buying_power":   float,  # cash * leverage (or equity for 1x)
#       "portfolio_value":float,
#       "currency":       str,    # "USD"
#       "broker":         str,    # "alpaca" / "hyperliquid" / ...
#       "status":         str,    # "ACTIVE" / "RESTRICTED" / ...
#       "raw":            Any,    # optional, broker-native payload
#   }
#
# Position:
#   {
#       "ticker":            str,   # canonical symbol ("AAPL", "BTC", "xyz:TSLA")
#       "shares":            float, # signed; negative = short
#       "avg_cost":          float,
#       "current_price":     float,
#       "market_value":      float,
#       "unrealized_pnl":    float,
#       "unrealized_pnl_pct":float,
#       "leverage":          float, # 1.0 for spot/equity
#       "broker":            str,
#   }
#
# OrderResult:
#   {
#       "order_id":     str,
#       "ticker":       str,
#       "side":         "buy" | "sell",
#       "qty":          float,
#       "type":         "market" | "limit" | "stop" | "stop_limit",
#       "status":       str,     # broker-native: accepted / filled / rejected / ...
#       "submitted_at": str,     # ISO timestamp
#       "broker":       str,
#       "error":        str | None,
#   }
#
# Candle (OHLCV):
#   {"t": iso_str, "open": float, "high": float, "low": float, "close": float, "volume": float}


@runtime_checkable
class BrokerProtocol(Protocol):
    """
    Structural interface every broker adapter must satisfy.

    Use ``runtime_checkable`` so ``isinstance(obj, BrokerProtocol)`` works for
    duck-typed adapters that haven't inherited explicitly.
    """

    #: human-readable broker id, e.g. "alpaca" or "hyperliquid"
    name: str

    #: list of asset classes supported ("equity", "perp", "hip3", "spot_crypto")
    asset_classes: list[str]

    def get_account(self) -> dict[str, Any]:
        """Return the canonical Account dict (see module docstring)."""
        ...

    def get_positions(self) -> list[dict[str, Any]]:
        """Return a list of canonical Position dicts, empty list if flat."""
        ...

    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str = "day",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit an order and return the canonical OrderResult dict."""
        ...

    def close_position(self, ticker: str) -> dict[str, Any]:
        """Flatten a specific position. Returns OrderResult dict."""
        ...

    def get_candles(
        self,
        ticker: str,
        interval: str = "1d",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return OHLCV candles as a list of Candle dicts, oldest-first."""
        ...


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------


class BrokerRouter:
    """
    Route tickers / asset classes to the appropriate registered broker.

    Usage:
        router = BrokerRouter()
        router.register(alpaca_bot, asset_classes=["equity"])
        router.register(hl_bot,     asset_classes=["perp", "hip3"])

        broker = router.for_ticker("AAPL")     # -> alpaca_bot
        broker = router.for_ticker("BTC")      # -> hl_bot
        broker = router.for_ticker("xyz:TSLA") # -> hl_bot
    """

    # Tickers starting with these prefixes are considered Hyperliquid HIP-3 tradfi.
    HIP3_PREFIXES = ("xyz:",)

    # Well-known crypto perp symbols (non-exhaustive; fallback to equity otherwise).
    CRYPTO_PERPS = {
        "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "BNB", "MATIC",
        "DOGE", "LINK", "LTC", "ADA", "ATOM", "NEAR", "APT", "SUI",
    }

    def __init__(self) -> None:
        self._brokers: list[tuple[BrokerProtocol, list[str]]] = []

    def register(self, broker: BrokerProtocol, asset_classes: list[str] | None = None) -> None:
        if asset_classes is None:
            asset_classes = list(getattr(broker, "asset_classes", []) or [])
        self._brokers.append((broker, asset_classes))

    def classify_ticker(self, ticker: str) -> str:
        """Return the asset class for a ticker: perp / hip3 / equity."""
        t = ticker.strip()
        if any(t.startswith(p) for p in self.HIP3_PREFIXES):
            return "hip3"
        if t.upper() in self.CRYPTO_PERPS:
            return "perp"
        return "equity"

    def for_ticker(self, ticker: str) -> BrokerProtocol | None:
        """Return the first registered broker that can handle this ticker."""
        asset_class = self.classify_ticker(ticker)
        for broker, classes in self._brokers:
            if asset_class in classes:
                return broker
        # Fallback: first registered broker
        return self._brokers[0][0] if self._brokers else None

    def all_brokers(self) -> list[BrokerProtocol]:
        return [b for b, _ in self._brokers]
