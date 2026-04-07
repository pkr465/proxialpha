"""
Sector Rotation Strategy - Rotates capital into the strongest sectors
and out of the weakest. Uses relative strength and momentum across sectors.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType
from core.config import WATCHLIST


class SectorRotationStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'rotation_lookback': 30,
            'top_sectors': 2,
            'bottom_sectors': 1,
            'min_sector_momentum': 0.02,
            'rebalance_threshold': 0.05,
        }
        if params:
            defaults.update(params)
        super().__init__("SectorRotation", weight, defaults)
        self._sector_rankings = {}

    def analyze_sectors(self, all_data: dict):
        """Rank sectors by momentum."""
        sector_returns = {}
        lb = self.params['rotation_lookback']
        for ticker, df in all_data.items():
            if len(df) < lb + 5 or ticker not in WATCHLIST:
                continue
            sector = WATCHLIST[ticker].get('sector', 'Unknown')
            ret = float((df['Close'].iloc[-1] - df['Close'].iloc[-lb]) / df['Close'].iloc[-lb])
            sector_returns.setdefault(sector, []).append((ticker, ret))

        self._sector_rankings = {}
        for sector, rets in sector_returns.items():
            avg_ret = np.mean([r for _, r in rets])
            self._sector_rankings[sector] = {
                'avg_return': avg_ret,
                'stocks': sorted(rets, key=lambda x: x[1], reverse=True),
            }

        self._sector_rankings = dict(sorted(
            self._sector_rankings.items(),
            key=lambda x: x[1]['avg_return'], reverse=True
        ))

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if not self._sector_rankings or ticker not in WATCHLIST:
            return []

        sector = WATCHLIST[ticker].get('sector', 'Unknown')
        if sector not in self._sector_rankings:
            return []

        price = float(df['Close'].iloc[-1])
        sectors = list(self._sector_rankings.keys())
        rank = sectors.index(sector) if sector in sectors else len(sectors)
        total = len(sectors)
        sector_data = self._sector_rankings[sector]
        avg_ret = sector_data['avg_return']

        signals = []
        reasons = []

        # Top sectors = buy
        if rank < self.params['top_sectors'] and avg_ret > self.params['min_sector_momentum']:
            # Find the stock's rank within its sector
            stock_rets = [r for t, r in sector_data['stocks']]
            stock_rank = next((i for i, (t, _) in enumerate(sector_data['stocks']) if t == ticker), -1)

            confidence = 0.6 + (1 - rank / max(total, 1)) * 0.2
            if stock_rank == 0:
                confidence += 0.1
                reasons.append(f"Top stock in top sector ({sector})")
            else:
                reasons.append(f"In top sector ({sector}, rank #{rank+1}/{total})")
            reasons.append(f"Sector momentum: +{avg_ret:.1%}")

            atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.BUY, confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * 3, 2),
                stop_loss=round(price - atr * 2, 2),
                position_size_pct=0.05,
                reasoning="; ".join(reasons),
                metadata={'sector_rank': rank + 1, 'sector_return': round(avg_ret, 4)},
            ))

        # Bottom sectors = sell
        elif rank >= total - self.params['bottom_sectors'] and avg_ret < -self.params['min_sector_momentum']:
            reasons.append(f"Weakest sector ({sector}, rank #{rank+1}/{total})")
            reasons.append(f"Sector momentum: {avg_ret:.1%}")
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.65,
                strategy_name=self.name, price=price,
                reasoning="; ".join(reasons),
            ))

        return signals
