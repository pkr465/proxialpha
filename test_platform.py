#!/usr/bin/env python3
"""
Platform Test - Runs all components with demo data.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.demo_data import generate_all_demo_data
from core.data_engine import calculate_technical_indicators, analyze_pullback
from core.config import WATCHLIST
from strategies.strategy_manager import StrategyManager
from strategies.dip_buyer import DipBuyerStrategy
from strategies.technical import TechnicalStrategy
from strategies.dca import DCAStrategy
from strategies.custom_rules import CustomRulesStrategy
from strategies.ai_strategy import AIStrategy
from backtesting.engine import BacktestEngine
import pandas as pd

print("=" * 60)
print("  PROXIALPHA - INTEGRATION TEST")
print("=" * 60)

# 1. Generate demo data
print("\n[1/5] Generating demo data...")
raw_data = generate_all_demo_data()
print(f"  Generated data for {len(raw_data)} stocks")

# 2. Calculate indicators
print("\n[2/5] Computing technical indicators...")
enriched = {}
analysis = []
for ticker, df in raw_data.items():
    df = calculate_technical_indicators(df)
    enriched[ticker] = df
    a = analyze_pullback(ticker, df)
    analysis.append(a)

analysis_df = pd.DataFrame(analysis).sort_values('drawdown_pct')
print(analysis_df[['ticker','sector','current_price','drawdown_pct','recovery_pct','rsi']].to_string(index=False))

# 3. Build strategy manager
print("\n[3/5] Loading strategies...")
manager = StrategyManager()
manager.register_strategy(DipBuyerStrategy(weight=1.2))
manager.register_strategy(TechnicalStrategy(weight=1.0))
manager.register_strategy(DCAStrategy(weight=0.8))
manager.register_strategy(CustomRulesStrategy(weight=0.9))
print(f"  Strategies: {list(manager.strategies.keys())}")

# 4. Scan for signals
print("\n[4/5] Scanning for signals...")
scan_results = manager.scan_all_tickers(enriched)
print(scan_results.to_string(index=False))

buys = scan_results[scan_results['Signal'].isin(['BUY', 'STRONG_BUY'])]
sells = scan_results[scan_results['Signal'].isin(['SELL', 'STRONG_SELL'])]
print(f"\n  BUY signals: {len(buys)} | SELL signals: {len(sells)} | HOLD: {len(scan_results) - len(buys) - len(sells)}")

# 5. Run backtest
print("\n[5/5] Running backtest...")
engine = BacktestEngine(manager, initial_capital=100000)
results = engine.run(enriched)
summary = engine.get_summary()

print("\n  BACKTEST RESULTS:")
for k, v in summary.items():
    print(f"    {k:25s}: {v}")

# Strategy config export
config_json = manager.export_config()
print(f"\n  Strategy config (JSON):\n{config_json[:200]}...")

print("\n" + "=" * 60)
print("  ALL TESTS PASSED")
print("=" * 60)
