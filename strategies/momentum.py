"""
Momentum Strategy - Buys stocks with strong recent price momentum.
Tracks rate of change, relative strength, and acceleration.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class MomentumStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'lookback_short': 20,
            'lookback_long': 60,
            'roc_threshold': 0.05,
            'acceleration_weight': 2,
            'volume_confirm': True,
            'min_volume_ratio': 1.2,
            'stop_loss_pct': 0.07,
            'trailing_stop_pct': 0.10,
        }
        if params:
            defaults.update(params)
        super().__init__("Momentum", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < self.params['lookback_long'] + 10:
            return []

        close = df['Close']
        price = float(close.iloc[-1])
        lb_s = self.params['lookback_short']
        lb_l = self.params['lookback_long']

        # Rate of change
        roc_short = float((close.iloc[-1] - close.iloc[-lb_s]) / close.iloc[-lb_s])
        roc_long = float((close.iloc[-1] - close.iloc[-lb_l]) / close.iloc[-lb_l])

        # Momentum acceleration (short ROC increasing faster than long ROC)
        prev_roc_short = float((close.iloc[-2] - close.iloc[-lb_s-1]) / close.iloc[-lb_s-1])
        acceleration = roc_short - prev_roc_short

        # Relative strength vs moving average
        sma50 = float(df['SMA_50'].iloc[-1]) if 'SMA_50' in df.columns and pd.notna(df['SMA_50'].iloc[-1]) else price
        rs_ratio = price / sma50 if sma50 > 0 else 1

        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1.0

        # Scoring
        score = 0
        reasons = []

        if roc_short > self.params['roc_threshold']:
            score += 2
            reasons.append(f"Short momentum +{roc_short:.1%}")
        elif roc_short < -self.params['roc_threshold']:
            score -= 2
            reasons.append(f"Short momentum {roc_short:.1%}")

        if roc_long > self.params['roc_threshold'] * 2:
            score += 2
            reasons.append(f"Long momentum +{roc_long:.1%}")
        elif roc_long < -self.params['roc_threshold'] * 2:
            score -= 2

        if acceleration > 0.005:
            score += self.params['acceleration_weight']
            reasons.append(f"Accelerating (+{acceleration:.3f})")
        elif acceleration < -0.005:
            score -= 1

        if rs_ratio > 1.05:
            score += 1
            reasons.append(f"RS ratio {rs_ratio:.2f}")
        elif rs_ratio < 0.95:
            score -= 1

        if self.params['volume_confirm'] and vol_ratio > self.params['min_volume_ratio'] and score > 0:
            score += 1
            reasons.append(f"Volume confirmed ({vol_ratio:.1f}x)")

        signals = []
        if score >= 4:
            atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02
            confidence = min(0.5 + (score - 4) * 0.1, 0.95)
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_BUY if score >= 6 else SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                price=price,
                target_price=round(price * (1 + self.params['trailing_stop_pct'] * 2), 2),
                stop_loss=round(price - atr * 1.5, 2),
                position_size_pct=round(0.03 + confidence * 0.05, 3),
                reasoning="; ".join(reasons),
                metadata={'roc_short': round(roc_short, 4), 'roc_long': round(roc_long, 4), 'acceleration': round(acceleration, 4)},
            ))
        elif score <= -4:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.7,
                strategy_name=self.name, price=price,
                reasoning="; ".join(reasons) or "Negative momentum",
            ))

        return signals
