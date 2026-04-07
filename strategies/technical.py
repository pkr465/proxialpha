"""
Technical Indicators Strategy - RSI, MACD, Moving Averages, Bollinger Bands.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType
from core.config import RSI_OVERSOLD, RSI_OVERBOUGHT, STOP_LOSS_PCT


class TechnicalStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'rsi_oversold': RSI_OVERSOLD,
            'rsi_overbought': RSI_OVERBOUGHT,
            'require_macd_confirm': True,
            'require_volume_confirm': True,
            'bb_squeeze_threshold': 0.02,
            'stop_loss_pct': STOP_LOSS_PCT,
        }
        if params:
            defaults.update(params)
        super().__init__("Technical", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < 50:
            return []

        signals = []
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        price = float(latest['Close'])
        rsi = float(latest.get('RSI', 50)) if pd.notna(latest.get('RSI')) else 50
        macd_hist = float(latest.get('MACD_Hist', 0)) if pd.notna(latest.get('MACD_Hist')) else 0
        prev_macd_hist = float(prev.get('MACD_Hist', 0)) if pd.notna(prev.get('MACD_Hist')) else 0
        sma20 = float(latest.get('SMA_20', price)) if pd.notna(latest.get('SMA_20')) else price
        sma50 = float(latest.get('SMA_50', price)) if pd.notna(latest.get('SMA_50')) else price
        bb_lower = float(latest.get('BB_Lower', 0)) if pd.notna(latest.get('BB_Lower')) else 0
        bb_upper = float(latest.get('BB_Upper', 0)) if pd.notna(latest.get('BB_Upper')) else 0
        vol_ratio = float(latest.get('Vol_Ratio', 1)) if pd.notna(latest.get('Vol_Ratio')) else 1

        # Score-based system: accumulate bullish/bearish points
        bull_score = 0
        bear_score = 0
        reasons = []

        # RSI signals
        if rsi < self.params['rsi_oversold']:
            bull_score += 3
            reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi < 40:
            bull_score += 1
            reasons.append(f"RSI low ({rsi:.0f})")
        elif rsi > self.params['rsi_overbought']:
            bear_score += 3
            reasons.append(f"RSI overbought ({rsi:.0f})")
        elif rsi > 60:
            bear_score += 1

        # MACD crossover
        if macd_hist > 0 and prev_macd_hist <= 0:
            bull_score += 3
            reasons.append("MACD bullish crossover")
        elif macd_hist > 0:
            bull_score += 1
        elif macd_hist < 0 and prev_macd_hist >= 0:
            bear_score += 3
            reasons.append("MACD bearish crossover")
        elif macd_hist < 0:
            bear_score += 1

        # Moving average alignment
        if price > sma20 > sma50:
            bull_score += 2
            reasons.append("Price > SMA20 > SMA50 (bullish alignment)")
        elif price < sma20 < sma50:
            bear_score += 2
            reasons.append("Price < SMA20 < SMA50 (bearish alignment)")

        # Golden/Death cross
        prev_sma20 = float(prev.get('SMA_20', 0)) if pd.notna(prev.get('SMA_20')) else 0
        prev_sma50 = float(prev.get('SMA_50', 0)) if pd.notna(prev.get('SMA_50')) else 0
        if prev_sma20 <= prev_sma50 and sma20 > sma50:
            bull_score += 3
            reasons.append("Golden cross (SMA20 x SMA50)")
        elif prev_sma20 >= prev_sma50 and sma20 < sma50:
            bear_score += 3
            reasons.append("Death cross (SMA20 x SMA50)")

        # Bollinger Band touch
        if bb_lower > 0 and price <= bb_lower * 1.01:
            bull_score += 2
            reasons.append("Price at lower Bollinger Band")
        elif bb_upper > 0 and price >= bb_upper * 0.99:
            bear_score += 2
            reasons.append("Price at upper Bollinger Band")

        # Volume confirmation
        if vol_ratio > 1.5:
            reasons.append(f"High volume ({vol_ratio:.1f}x avg)")
            if bull_score > bear_score:
                bull_score += 1
            elif bear_score > bull_score:
                bear_score += 1

        # Generate signal based on scores
        net_score = bull_score - bear_score
        if net_score >= 5:
            confidence = min(0.5 + (net_score - 5) * 0.1, 0.95)
            atr = float(latest.get('ATR', price * 0.02)) if pd.notna(latest.get('ATR')) else price * 0.02
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_BUY if net_score >= 8 else SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                price=price,
                target_price=round(price + (atr * 3), 2),
                stop_loss=round(price - (atr * 1.5), 2),
                position_size_pct=round(0.03 + (confidence * 0.05), 3),
                reasoning="; ".join(reasons),
                metadata={'bull_score': bull_score, 'bear_score': bear_score, 'rsi': rsi},
            ))
        elif net_score <= -5:
            confidence = min(0.5 + (abs(net_score) - 5) * 0.1, 0.95)
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_SELL if net_score <= -8 else SignalType.SELL,
                confidence=round(confidence, 2),
                strategy_name=self.name,
                price=price,
                reasoning="; ".join(reasons),
                metadata={'bull_score': bull_score, 'bear_score': bear_score, 'rsi': rsi},
            ))
        else:
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.HOLD,
                confidence=0.5,
                strategy_name=self.name,
                price=price,
                reasoning="; ".join(reasons) if reasons else "No strong signals",
                metadata={'bull_score': bull_score, 'bear_score': bear_score, 'rsi': rsi},
            ))

        return signals
