"""
Step 1: Import smoke test.
Verifies every integrated module loads cleanly with no syntax/import errors.
Exits non-zero on failure so smoke.sh can chain it.
"""
from __future__ import annotations
import sys
import traceback

MODULES = [
    # core
    "core.risk_manager",
    "core.indicators",
    "core.ai_decision_maker",
    "core.diary",
    "core.llm_adapter",
    "core.data_engine",
    # strategies
    "strategies.base",
    "strategies.strategy_manager",
    "strategies.ai_strategy",
    "strategies.dip_buyer",
    # execution
    "backtesting.engine",
    "paper_trading.simulator",
    "live_trading.broker_protocol",
    "live_trading.alpaca_bot",
    "live_trading.hyperliquid_bot",
    # api
    "api.server",
]

def main() -> int:
    failed = []
    for name in MODULES:
        try:
            __import__(name)
            print(f"  OK   {name}")
        except Exception as e:
            failed.append((name, e))
            print(f"  FAIL {name}: {e}")
            traceback.print_exc()

    if failed:
        print(f"\n[test_imports] {len(failed)} module(s) failed to import.")
        return 1
    print(f"\n[test_imports] All {len(MODULES)} modules imported OK.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
