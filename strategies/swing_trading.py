"""
Swing Trading Strategy - Multi-day to multi-week trades capturing price swings.
Uses support/resistance levels, Fibonacci retracements, and trend confirmation.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class SwingTradingStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'swing_lookback': 20,
            'support_resistance_lookback': 60,
            'fib_levels': [0.236, 0.382, 0.500, 0.618, 0.786],
            'fib_tolerance': 0.015,
            'min_swing_size_pct': 0.05,
            'confirmation_candles': 2,
            'stop_loss_atr_mult': 2.0,
            'target_atr_mult': 4.0,
        }
        if params:
            defaults.update(params)
        super().__init__("SwingTrading", weight, defaults)

    def _find_swing_points(self, df, lookback=5):
        """Find local highs and lows."""
        highs, lows = [], []
        for i in range(lookback, len(df) - lookback):
            if all(df['High'].iloc[i] >= df['High'].iloc[i-lookback:i]) and \
               all(df['High'].iloc[i] >= df['High'].iloc[i+1:i+lookback+1]):
                highs.append((i, float(df['High'].iloc[i])))
            if all(df['Low'].iloc[i] <= df['Low'].iloc[i-lookback:i]) and \
               all(df['Low'].iloc[i] <= df['Low'].iloc[i+1:i+lookback+1]):
                lows.append((i, float(df['Low'].iloc[i])))
        return highs, lows

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < self.params['support_resistance_lookback'] + 20:
            return []

        price = float(df['Close'].iloc[-1])
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02
        rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns and pd.notna(df['RSI'].iloc[-1]) else 50

        # Find swing points
        highs, lows = self._find_swing_points(df.iloc[-self.params['support_resistance_lookback']:])

        if not highs or not lows:
            return []

        # Recent swing high/low
        recent_high = max(h[1] for h in highs[-3:]) if highs else price * 1.1
        recent_low = min(l[1] for l in lows[-3:]) if lows else price * 0.9
        swing_range = recent_high - recent_low

        if swing_range / recent_low < self.params['min_swing_size_pct']:
            return []

        # Fibonacci retracement levels
        fib_levels = {level: recent_high - (swing_range * level) for level in self.params['fib_levels']}

        # Check if price is near a Fibonacci level
        near_fib = None
        for level, fib_price in fib_levels.items():
            if abs(price - fib_price) / price < self.params['fib_tolerance']:
                near_fib = level
                break

        # Confirmation: last N candles showing reversal
        last_n = df.iloc[-self.params['confirmation_candles']:]
        bullish_candles = sum(1 for _, row in last_n.iterrows() if float(row['Close']) > float(row['Open']))
        bearish_candles = len(last_n) - bullish_candles

        score = 0
        reasons = []

        # Price near support (swing low) with bullish confirmation
        support_distance = (price - recent_low) / recent_low
        resistance_distance = (recent_high - price) / price

        if support_distance < 0.03 and bullish_candles >= self.params['confirmation_candles'] - 1:
            score += 3
            reasons.append(f"Near swing support ${recent_low:.2f} ({support_distance:.1%} away)")

        if resistance_distance < 0.03 and bearish_candles >= self.params['confirmation_candles'] - 1:
            score -= 3
            reasons.append(f"Near swing resistance ${recent_high:.2f}")

        # Fibonacci bounce
        if near_fib and bullish_candles >= 1 and rsi < 50:
            score += 2
            reasons.append(f"Fibonacci {near_fib:.1%} retracement (${fib_levels[near_fib]:.2f})")

        # RSI divergence from trend
        if rsi < 35 and support_distance < 0.10:
            score += 1
            reasons.append(f"RSI {rsi:.0f} at support zone")

        # Risk/reward check
        potential_reward = recent_high - price
        potential_risk = price - (price - atr * self.params['stop_loss_atr_mult'])
        rr_ratio = potential_reward / potential_risk if potential_risk > 0 else 0

        if rr_ratio > 2 and score > 0:
            score += 1
            reasons.append(f"R:R ratio {rr_ratio:.1f}:1")

        signals = []
        if score >= 3:
            confidence = min(0.5 + (score - 3) * 0.1, 0.9)
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.BUY, confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * self.params['target_atr_mult'], 2),
                stop_loss=round(price - atr * self.params['stop_loss_atr_mult'], 2),
                position_size_pct=0.05,
                reasoning="; ".join(reasons),
                metadata={'swing_high': recent_high, 'swing_low': recent_low, 'fib_level': near_fib, 'rr_ratio': round(rr_ratio, 2)},
            ))
        elif score <= -3:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.65,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))
        return signals
