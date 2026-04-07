"""
Earnings Play Strategy - Trades around earnings announcements.
Pre-earnings momentum, post-earnings drift, and IV crush plays.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from strategies.base import BaseStrategy, StrategySignal, SignalType


class EarningsPlayStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'pre_earnings_days': 10,
            'post_earnings_days': 5,
            'min_pre_earnings_momentum': 0.03,
            'earnings_surprise_threshold': 0.02,
            'volume_spike_threshold': 2.0,
            'avoid_holding_through': True,
            'earnings_calendar': {},  # ticker -> [dates]
        }
        if params:
            defaults.update(params)
        super().__init__("EarningsPlay", weight, defaults)

    def set_earnings_dates(self, calendar: dict):
        self.params['earnings_calendar'] = calendar

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < 30:
            return []

        price = float(df['Close'].iloc[-1])
        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1.0

        signals = []

        # Pre-earnings momentum detection (even without exact dates)
        # Detect unusual volume + momentum combo as potential pre-earnings run
        roc_10d = float((df['Close'].iloc[-1] - df['Close'].iloc[-10]) / df['Close'].iloc[-10]) if len(df) >= 10 else 0
        roc_5d = float((df['Close'].iloc[-1] - df['Close'].iloc[-5]) / df['Close'].iloc[-5]) if len(df) >= 5 else 0

        # Check for earnings date proximity
        today = df.index[-1]
        earnings_dates = self.params['earnings_calendar'].get(ticker, [])
        days_to_earnings = None
        days_since_earnings = None
        for ed in earnings_dates:
            ed_dt = pd.Timestamp(ed)
            delta = (ed_dt - today).days
            if 0 < delta <= self.params['pre_earnings_days']:
                days_to_earnings = delta
            elif -self.params['post_earnings_days'] <= delta <= 0:
                days_since_earnings = abs(delta)

        reasons = []
        score = 0

        # Pre-earnings momentum play
        if days_to_earnings and roc_10d > self.params['min_pre_earnings_momentum']:
            score += 2
            reasons.append(f"Pre-earnings momentum: +{roc_10d:.1%} over 10d, {days_to_earnings}d to earnings")
            if vol_ratio > 1.5:
                score += 1
                reasons.append(f"Rising volume ({vol_ratio:.1f}x)")

        # Post-earnings drift (volume spike + momentum continuation)
        if days_since_earnings is not None and days_since_earnings <= 3:
            if vol_ratio > self.params['volume_spike_threshold']:
                if roc_5d > 0.03:
                    score += 3
                    reasons.append(f"Post-earnings drift: +{roc_5d:.1%}, vol {vol_ratio:.1f}x")
                elif roc_5d < -0.03:
                    score -= 3
                    reasons.append(f"Post-earnings sell-off: {roc_5d:.1%}, vol {vol_ratio:.1f}x")

        # Detect potential pre-earnings run without calendar (heuristic)
        if not earnings_dates and vol_ratio > 2.0 and abs(roc_5d) > 0.05:
            if roc_5d > 0:
                score += 1
                reasons.append(f"Unusual activity: vol {vol_ratio:.1f}x, +{roc_5d:.1%} in 5d (possible pre-earnings)")

        if score >= 3:
            confidence = min(0.5 + (score - 3) * 0.1, 0.85)
            atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.03
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.BUY, confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * 2, 2),
                stop_loss=round(price - atr * 1.5, 2),
                position_size_pct=0.03,
                reasoning="; ".join(reasons),
                metadata={'days_to_earnings': days_to_earnings, 'roc_5d': round(roc_5d, 4), 'roc_10d': round(roc_10d, 4)},
            ))
        elif score <= -3:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.7,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))

        return signals
