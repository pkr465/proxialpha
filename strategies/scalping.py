"""
Scalping Strategy - Short-term intraday-style trades using micro price action,
volume spikes, and tight risk management. Works on daily data as swing scalps.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class ScalpingStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'lookback': 5,
            'min_vol_spike': 2.0,
            'rsi_oversold': 25,
            'rsi_overbought': 75,
            'atr_target_mult': 1.0,
            'atr_stop_mult': 0.75,
            'max_hold_days': 3,
            'tight_bb_threshold': 0.03,
        }
        if params:
            defaults.update(params)
        super().__init__("Scalping", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < 20:
            return []

        price = float(df['Close'].iloc[-1])
        prev = float(df['Close'].iloc[-2])
        rsi = float(df['RSI'].iloc[-1]) if 'RSI' in df.columns and pd.notna(df['RSI'].iloc[-1]) else 50
        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02

        bb_lower = float(df['BB_Lower'].iloc[-1]) if 'BB_Lower' in df.columns and pd.notna(df['BB_Lower'].iloc[-1]) else 0
        bb_upper = float(df['BB_Upper'].iloc[-1]) if 'BB_Upper' in df.columns and pd.notna(df['BB_Upper'].iloc[-1]) else 0
        bb_mid = float(df['BB_Mid'].iloc[-1]) if 'BB_Mid' in df.columns and pd.notna(df['BB_Mid'].iloc[-1]) else price

        # Candle analysis
        open_p = float(df['Open'].iloc[-1])
        high_p = float(df['High'].iloc[-1])
        low_p = float(df['Low'].iloc[-1])
        body = abs(price - open_p)
        wick_upper = high_p - max(price, open_p)
        wick_lower = min(price, open_p) - low_p
        is_hammer = wick_lower > body * 2 and wick_upper < body * 0.5 and price > open_p
        is_shooting_star = wick_upper > body * 2 and wick_lower < body * 0.5 and price < open_p

        score = 0
        reasons = []

        # Volume spike + RSI extreme = scalp entry
        if vol_ratio > self.params['min_vol_spike'] and rsi < self.params['rsi_oversold']:
            score += 3
            reasons.append(f"Volume spike ({vol_ratio:.1f}x) + RSI oversold ({rsi:.0f})")

        if vol_ratio > self.params['min_vol_spike'] and rsi > self.params['rsi_overbought']:
            score -= 3
            reasons.append(f"Volume spike ({vol_ratio:.1f}x) + RSI overbought ({rsi:.0f})")

        # Hammer reversal
        if is_hammer and rsi < 40:
            score += 2
            reasons.append("Hammer candle at low RSI")

        if is_shooting_star and rsi > 60:
            score -= 2
            reasons.append("Shooting star at high RSI")

        # Price at Bollinger Band extremes
        if bb_lower > 0 and price <= bb_lower and vol_ratio > 1.5:
            score += 2
            reasons.append("Price at lower BB with volume")

        # Mean reversion micro
        pct_from_mid = (price - bb_mid) / bb_mid if bb_mid > 0 else 0
        if pct_from_mid < -0.03 and rsi < 35:
            score += 1
            reasons.append(f"Below BB mid by {pct_from_mid:.1%}")

        signals = []
        if score >= 3:
            confidence = min(0.5 + (score - 3) * 0.12, 0.85)
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.BUY, confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * self.params['atr_target_mult'], 2),
                stop_loss=round(price - atr * self.params['atr_stop_mult'], 2),
                position_size_pct=0.02,  # Small size for scalps
                reasoning="; ".join(reasons),
                metadata={'hold_days': self.params['max_hold_days'], 'vol_ratio': round(vol_ratio, 2)},
            ))
        elif score <= -3:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.65,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))
        return signals
