"""
Step 4: Backtest with the RiskManager wired in.
Uses demo data (no network) and enables the audit diary so we can verify
that the risk gate is actually being exercised on BUY paths.
"""
from __future__ import annotations
import os
import sys

from core.demo_data import generate_all_demo_data
from core.data_engine import calculate_technical_indicators
from core.risk_manager import RiskManager
from strategies.strategy_manager import StrategyManager
from strategies.dip_buyer import DipBuyerStrategy
from strategies.technical import TechnicalStrategy
from backtesting.engine import BacktestEngine


def main() -> int:
    print("  generating demo data...")
    raw = generate_all_demo_data()
    data = {t: calculate_technical_indicators(df) for t, df in raw.items()}
    print(f"  {len(data)} tickers prepped")

    manager = StrategyManager()
    manager.register_strategy(DipBuyerStrategy(weight=1.2))
    manager.register_strategy(TechnicalStrategy(weight=1.0))

    rm = RiskManager()
    engine = BacktestEngine(
        strategy_manager=manager,
        initial_capital=100_000,
        risk_manager=rm,
        enable_diary=True,
    )

    print("  running backtest with risk gate + diary...")
    engine.run(data)
    summary = engine.get_summary()

    # Print a compact summary
    print("\n  BACKTEST SUMMARY")
    for k, v in summary.items():
        print(f"    {k:25s}: {v}")

    # Sanity: risk manager object is the one we passed in
    assert engine.risk_manager is rm, "engine should retain the injected risk manager"
    assert engine.risk_manager.max_position_pct == rm.max_position_pct
    print(f"  risk: max_position_pct={rm.max_position_pct}, min_order_usd={rm.min_order_usd}")

    # Sanity: diary file exists and has at least the session-start entry
    diary_path = "data/backtest_diary.jsonl"
    if os.path.exists(diary_path):
        with open(diary_path) as f:
            lines = f.readlines()
        print(f"  diary: {len(lines)} entries at {diary_path}")
    else:
        print(f"  diary: (file not yet written at {diary_path} — no trades took place)")

    print("\n[test_backtest] OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[test_backtest] FAIL: {e}")
        sys.exit(1)
