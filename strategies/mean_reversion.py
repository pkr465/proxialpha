"""
Mean Reversion Strategy - Buys when price deviates significantly below its mean,
sells when it reverts. Uses z-scores, Bollinger Bands, and RSI extremes.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'z_score_threshold': -2.0,
            'z_score_exit': 0.0,
            'lookback': 50,
            'bb_touch_weight': 2,
            'rsi_extreme_weight': 2,
            'min_mean_distance_pct': 0.05,
            'stop_loss_pct': 0.10,
        }
        if params:
            defaults.update(params)
        super().__init__("MeanReversion", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < self.params['lookback'] + 10:
            return []

        close = df['Close']
        price = float(close.iloc[-1])
        lb = self.params['lookback']

        mean = float(close.rolling(lb).mean().iloc[-1])
        std = float(close.rolling(lb).std().iloc[-1])
        z_score = (price - mean) / std if std > 0 else 0

        rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns and pd.notna(df['RSI'].iloc[-1]) else 50
        bb_lower = float(df['BB_Lower'].iloc[-1]) if 'BB_Lower' in df.columns and pd.notna(df['BB_Lower'].iloc[-1]) else 0
        bb_upper = float(df['BB_Upper'].iloc[-1]) if 'BB_Upper' in df.columns and pd.notna(df['BB_Upper'].iloc[-1]) else 0

        score = 0
        reasons = []

        # Z-score signals
        if z_score <= self.params['z_score_threshold']:
            score += 3
            reasons.append(f"Z-score {z_score:.2f} (oversold)")
        elif z_score <= -1.5:
            score += 1
            reasons.append(f"Z-score {z_score:.2f}")
        elif z_score >= abs(self.params['z_score_threshold']):
            score -= 3
            reasons.append(f"Z-score {z_score:.2f} (overbought)")

        # Bollinger Band
        if bb_lower > 0 and price <= bb_lower:
            score += self.params['bb_touch_weight']
            reasons.append("Below lower Bollinger Band")
        elif bb_upper > 0 and price >= bb_upper:
            score -= self.params['bb_touch_weight']
            reasons.append("Above upper Bollinger Band")

        # RSI extremes
        if rsi < 25:
            score += self.params['rsi_extreme_weight']
            reasons.append(f"RSI extreme low ({rsi:.0f})")
        elif rsi > 75:
            score -= self.params['rsi_extreme_weight']
            reasons.append(f"RSI extreme high ({rsi:.0f})")

        # Distance from mean
        distance_pct = (mean - price) / mean if mean > 0 else 0
        if distance_pct > self.params['min_mean_distance_pct']:
            score += 1
            reasons.append(f"Below mean by {distance_pct:.1%}")

        signals = []
        if score >= 4:
            confidence = min(0.5 + (score - 4) * 0.1, 0.95)
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_BUY if score >= 6 else SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(mean, 2),
                stop_loss=round(price * (1 - self.params['stop_loss_pct']), 2),
                position_size_pct=round(0.03 + confidence * 0.04, 3),
                reasoning="; ".join(reasons),
                metadata={'z_score': round(z_score, 2), 'mean': round(mean, 2), 'distance_pct': round(distance_pct, 4)},
            ))
        elif score <= -4:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.7,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))

        return signals
