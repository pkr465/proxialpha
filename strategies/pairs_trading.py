"""
Pairs Trading Strategy - Statistical arbitrage between correlated stocks.
Finds cointegrated pairs and trades the spread when it diverges.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class PairsTradingStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'correlation_threshold': 0.7,
            'z_score_entry': 2.0,
            'z_score_exit': 0.5,
            'lookback': 60,
            'sector_pairs_only': True,
            'predefined_pairs': [
                ('COIN', 'HOOD'), ('NKE', 'LULU'), ('NVO', 'UNH'),
                ('TGT', 'NKE'), ('ORCL', 'NOW'), ('HIMS', 'NVO'),
            ],
        }
        if params:
            defaults.update(params)
        super().__init__("PairsTrading", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        # Pairs trading signals are generated in scan_pairs() below
        # Individual ticker calls return any pending signals
        return getattr(self, '_pending_signals', {}).get(ticker, [])

    def scan_pairs(self, all_data: dict) -> dict:
        """Scan all pairs and generate spread-based signals."""
        self._pending_signals = {}
        lb = self.params['lookback']

        for t1, t2 in self.params['predefined_pairs']:
            if t1 not in all_data or t2 not in all_data:
                continue
            df1, df2 = all_data[t1], all_data[t2]
            if len(df1) < lb + 10 or len(df2) < lb + 10:
                continue

            # Align dates
            common = df1.index.intersection(df2.index)[-lb:]
            if len(common) < lb:
                continue

            s1 = df1.loc[common, 'Close'].values.astype(float)
            s2 = df2.loc[common, 'Close'].values.astype(float)

            # Correlation check
            corr = float(np.corrcoef(s1, s2)[0, 1])
            if abs(corr) < self.params['correlation_threshold']:
                continue

            # Spread z-score
            ratio = s1 / s2
            mean_ratio = ratio.mean()
            std_ratio = ratio.std()
            z_score = (ratio[-1] - mean_ratio) / std_ratio if std_ratio > 0 else 0

            price1 = float(s1[-1])
            price2 = float(s2[-1])

            if z_score > self.params['z_score_entry']:
                # Spread too wide: short t1, long t2
                self._pending_signals.setdefault(t2, []).append(StrategySignal(
                    ticker=t2, signal=SignalType.BUY, confidence=min(0.6 + abs(z_score) * 0.1, 0.9),
                    strategy_name=self.name, price=price2,
                    target_price=round(price2 * (1 + 0.05), 2),
                    position_size_pct=0.04,
                    reasoning=f"Pairs trade: Long {t2} / Short {t1}. Spread z={z_score:.2f}, corr={corr:.2f}",
                    metadata={'pair': f'{t1}/{t2}', 'z_score': round(z_score, 2), 'correlation': round(corr, 2)},
                ))
            elif z_score < -self.params['z_score_entry']:
                # Spread too narrow: long t1, short t2
                self._pending_signals.setdefault(t1, []).append(StrategySignal(
                    ticker=t1, signal=SignalType.BUY, confidence=min(0.6 + abs(z_score) * 0.1, 0.9),
                    strategy_name=self.name, price=price1,
                    target_price=round(price1 * (1 + 0.05), 2),
                    position_size_pct=0.04,
                    reasoning=f"Pairs trade: Long {t1} / Short {t2}. Spread z={z_score:.2f}, corr={corr:.2f}",
                    metadata={'pair': f'{t1}/{t2}', 'z_score': round(z_score, 2), 'correlation': round(corr, 2)},
                ))

        return self._pending_signals
