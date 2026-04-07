"""
Breakout Strategy - Detects price breaking above resistance or below support
with volume confirmation. Tracks consolidation ranges and volatility squeezes.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class BreakoutStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'consolidation_days': 20,
            'breakout_threshold_pct': 0.02,
            'volume_surge_threshold': 1.8,
            'atr_filter': True,
            'bb_squeeze_lookback': 20,
            'bb_squeeze_threshold': 0.04,
            'stop_loss_atr_mult': 1.5,
            'target_atr_mult': 3.0,
        }
        if params:
            defaults.update(params)
        super().__init__("Breakout", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < self.params['consolidation_days'] + 10:
            return []

        close = df['Close']
        high = df['High']
        low = df['Low']
        price = float(close.iloc[-1])
        cd = self.params['consolidation_days']

        # Consolidation range (last N days)
        range_high = float(high.iloc[-cd:-1].max())
        range_low = float(low.iloc[-cd:-1].min())
        range_pct = (range_high - range_low) / range_low if range_low > 0 else 0

        # Bollinger Band width (squeeze detection)
        bb_upper = df.get('BB_Upper')
        bb_lower = df.get('BB_Lower')
        bb_mid = df.get('BB_Mid')
        bb_width = 0
        if bb_upper is not None and bb_lower is not None and bb_mid is not None:
            if pd.notna(bb_upper.iloc[-1]) and pd.notna(bb_lower.iloc[-1]) and pd.notna(bb_mid.iloc[-1]):
                bb_width = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_mid.iloc[-1])

        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1.0
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02

        score = 0
        reasons = []

        # Upward breakout
        breakout_up = price > range_high * (1 + self.params['breakout_threshold_pct'])
        breakout_down = price < range_low * (1 - self.params['breakout_threshold_pct'])

        if breakout_up:
            score += 3
            reasons.append(f"Broke above {cd}-day high ${range_high:.2f}")
        elif breakout_down:
            score -= 3
            reasons.append(f"Broke below {cd}-day low ${range_low:.2f}")

        # Bollinger squeeze (tight consolidation = bigger breakout potential)
        if bb_width > 0 and bb_width < self.params['bb_squeeze_threshold']:
            if breakout_up:
                score += 2
                reasons.append(f"BB squeeze ({bb_width:.3f})")
            elif breakout_down:
                score -= 2

        # Volume confirmation
        if vol_ratio > self.params['volume_surge_threshold']:
            if score > 0:
                score += 2
                reasons.append(f"Volume surge {vol_ratio:.1f}x")
            elif score < 0:
                score -= 2
                reasons.append(f"Volume surge {vol_ratio:.1f}x (bearish)")

        # Previous day close vs range (confirmation of breakout)
        prev_close = float(close.iloc[-2])
        if breakout_up and prev_close <= range_high:
            score += 1
            reasons.append("Fresh breakout (prev close inside range)")

        signals = []
        if score >= 4:
            confidence = min(0.5 + (score - 4) * 0.1, 0.95)
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_BUY if score >= 6 else SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * self.params['target_atr_mult'], 2),
                stop_loss=round(price - atr * self.params['stop_loss_atr_mult'], 2),
                position_size_pct=round(0.04 + confidence * 0.04, 3),
                reasoning="; ".join(reasons),
                metadata={'range_high': range_high, 'range_low': range_low, 'bb_width': round(bb_width, 4)},
            ))
        elif score <= -4:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.7,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))

        return signals
