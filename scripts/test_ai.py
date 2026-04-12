"""
Step 7: AIStrategy end-to-end test. SKIPS automatically if ANTHROPIC_API_KEY
is not set, so it's safe to include in smoke.sh.

When the key is present, this:
  - builds a synthetic OHLCV dataframe
  - instantiates AIStrategy with use_api=True, enable_tools=True
  - calls generate_signals() and checks we got a well-formed StrategySignal list
"""
from __future__ import annotations
import os
import sys

if not os.getenv("ANTHROPIC_API_KEY"):
    print("[test_ai] ANTHROPIC_API_KEY not set — skipping live LLM call")
    sys.exit(0)

import numpy as np
import pandas as pd

from strategies.ai_strategy import AIStrategy
from strategies.base import SignalType


def _synthetic_df(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    drift = np.linspace(100, 140, n)
    noise = rng.normal(0, 1.5, n)
    close = drift + noise
    high = close + np.abs(rng.normal(0, 0.8, n))
    low  = close - np.abs(rng.normal(0, 0.8, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    vol = rng.integers(500_000, 2_000_000, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def main() -> int:
    df = _synthetic_df()
    strat = AIStrategy(params={"use_api": True, "enable_tools": True})
    signals = strat.generate_signals("AAPL", df)

    print(f"  got {len(signals)} signals")
    for s in signals:
        print(f"    {s.ticker} {s.signal_type.name} conf={s.confidence} "
              f"meta_keys={list((s.metadata or {}).keys())}")
        assert isinstance(s.signal_type, SignalType)
        assert 0.0 <= s.confidence <= 1.0

    print("\n[test_ai] OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[test_ai] FAIL: {e}")
        sys.exit(1)
