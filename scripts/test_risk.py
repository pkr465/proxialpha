"""
Step 2: RiskManager unit checks.
Exercises the four guards that matter most for safe trading:
  1. allocation capping (max_position_pct)
  2. auto SL injection (mandatory_sl_pct)
  3. min order size auto-bump
  4. concurrent position limit
No network, no dataframes — pure function tests.
"""
from __future__ import annotations
import sys

from core.risk_manager import RiskManager


def _account(value: float = 100_000.0, cash: float = 100_000.0, positions=None) -> dict:
    return {
        "total_value": value,
        "equity": value,
        "balance": cash,
        "cash": cash,
        "positions": positions or [],
        "day_start_value": value,
    }


def test_allocation_cap() -> None:
    rm = RiskManager()
    ok, reason, adj = rm.validate_trade(
        {"ticker": "AAPL", "action": "buy", "allocation_usd": 50_000, "current_price": 150.0},
        _account(),
        initial_balance=100_000,
    )
    assert ok, f"expected pass, got {reason}"
    # 10% of 100k = 10k cap
    assert abs(adj["allocation_usd"] - 10_000) < 1e-6, f"cap failed: {adj['allocation_usd']}"
    # Auto SL at 8% below entry
    assert abs(adj["sl_price"] - 138.0) < 1e-6, f"sl wrong: {adj['sl_price']}"
    print("  OK   allocation cap 50k -> 10k, sl auto-set at 138.0")


def test_min_order_bump() -> None:
    rm = RiskManager()
    ok, reason, adj = rm.validate_trade(
        {"ticker": "AAPL", "action": "buy", "allocation_usd": 50.0, "current_price": 150.0},
        _account(),
        initial_balance=100_000,
    )
    # RiskManager BUMPS sub-minimum orders up to min_order_usd, it does not reject.
    assert ok, f"min-order-bump trade should pass, got: {reason}"
    assert adj["allocation_usd"] >= rm.min_order_usd, \
        f"expected bump to >= {rm.min_order_usd}, got {adj['allocation_usd']}"
    print(f"  OK   min order $50 bumped to ${adj['allocation_usd']}")


def test_concurrent_limit() -> None:
    rm = RiskManager()
    limit = rm.max_concurrent_positions
    positions = [
        {"ticker": f"SYM{i}", "shares": 1, "avg_cost": 10.0, "current_price": 10.0}
        for i in range(limit)
    ]
    ok, reason, _ = rm.validate_trade(
        {"ticker": "AAPL", "action": "buy", "allocation_usd": 1_000, "current_price": 150.0},
        _account(positions=positions),
        initial_balance=100_000,
    )
    assert not ok, "expected rejection at concurrent limit"
    print(f"  OK   concurrent limit ({limit}) rejected: {reason}")


def test_sell_passthrough() -> None:
    rm = RiskManager()
    ok, _, _ = rm.validate_trade(
        {"ticker": "AAPL", "action": "sell", "allocation_usd": 1, "current_price": 150.0},
        _account(),
        initial_balance=100_000,
    )
    assert ok, "sells should pass through"
    print("  OK   sell passthrough")


def test_risk_summary_shape() -> None:
    rm = RiskManager()
    s = rm.get_risk_summary()
    for key in (
        "max_position_pct", "max_loss_per_position_pct", "max_leverage",
        "max_total_exposure_pct", "daily_loss_circuit_breaker_pct",
        "mandatory_sl_pct", "max_concurrent_positions", "min_order_usd",
    ):
        assert key in s, f"missing {key} in risk summary"
    print(f"  OK   get_risk_summary() returned {len(s)} keys")


def main() -> int:
    try:
        test_allocation_cap()
        test_min_order_bump()
        test_concurrent_limit()
        test_sell_passthrough()
        test_risk_summary_shape()
    except AssertionError as e:
        print(f"\n[test_risk] FAIL: {e}")
        return 1
    print("\n[test_risk] All risk checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
