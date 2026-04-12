"""
Step 5: Paper trader risk gate check.
Calls execute_buy twice:
  (a) oversized $50k allocation -> should be capped to 10% of equity
  (b) a tiny allocation -> should be rejected by min_order_usd
Then verifies the diary got written to.
"""
from __future__ import annotations
import os
import sys
import tempfile

from core.risk_manager import RiskManager
from strategies.strategy_manager import StrategyManager
from paper_trading.simulator import PaperTrader


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()

    manager = StrategyManager()  # no strategies needed for direct execute_buy test
    pt = PaperTrader(
        strategy_manager=manager,
        initial_capital=100_000,
        state_file=tmp.name,
        risk_manager=RiskManager(),
    )

    # (a) Oversized: should be capped to 10% of equity (= $10k -> 66 shares at $150)
    result_big = pt.execute_buy(
        ticker="AAPL",
        price=150.0,
        dollar_amount=50_000,
        reason="oversized test",
    )
    print(f"  oversized buy: {result_big}")
    assert "error" not in result_big, f"oversized buy should have been capped, not rejected: {result_big}"
    assert result_big["shares"] * result_big["price"] <= 10_050, \
        f"cap failed: allocation {result_big['shares'] * result_big['price']}"
    print(f"  OK   capped to {result_big['shares']} shares (~${result_big['shares']*result_big['price']:.0f})")

    # (b) Invalid share count (sub-$300 on a $300 ticker -> 0 shares)
    result_small = pt.execute_buy(
        ticker="MSFT",
        price=300.0,
        dollar_amount=50,
        reason="zero-share test",
    )
    print(f"  zero-share buy: {result_small}")
    assert "error" in result_small, "zero-share buy should have been rejected"
    print(f"  OK   rejected: {result_small['error']}")

    # (c) Sell without position -> rejected
    result_sell = pt.execute_sell(
        ticker="NVDA",
        price=500.0,
        shares=10,
        reason="no-position test",
    )
    print(f"  no-position sell: {result_sell}")
    assert "error" in result_sell, "sell without position should have been rejected"
    print(f"  OK   rejected: {result_sell['error']}")

    # Diary exists
    diary_path = "data/paper_diary.jsonl"
    if os.path.exists(diary_path):
        with open(diary_path) as f:
            n = sum(1 for _ in f)
        print(f"  diary: {n} entries at {diary_path}")
    else:
        print("  diary: (not written — unexpected)")
        return 1

    # cleanup temp state file
    try: os.unlink(tmp.name)
    except OSError: pass

    print("\n[test_paper] OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"[test_paper] FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[test_paper] FAIL: {e}")
        sys.exit(1)
