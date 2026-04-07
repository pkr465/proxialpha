"""
Options Flow Strategy - Detects unusual options activity patterns from
volume and price action that suggest institutional positioning.
Uses equity-level proxies when options data isn't available.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class OptionsFlowStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'volume_spike_threshold': 2.5,
            'price_volume_divergence': True,
            'dark_pool_proxy': True,
            'implied_move_lookback': 20,
            'min_market_cap_proxy': 5,  # billion
            'institutional_accumulation_days': 5,
        }
        if params:
            defaults.update(params)
        super().__init__("OptionsFlow", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < 30:
            return []

        price = float(df['Close'].iloc[-1])
        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1
        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02

        score = 0
        reasons = []

        # Unusual volume detection (proxy for options sweep activity)
        if vol_ratio > self.params['volume_spike_threshold']:
            # Check if volume spike is on up day or down day
            daily_return = float((df['Close'].iloc[-1] - df['Close'].iloc[-2]) / df['Close'].iloc[-2])
            if daily_return > 0:
                score += 3
                reasons.append(f"Unusual volume ({vol_ratio:.1f}x) on up day (+{daily_return:.1%})")
            elif daily_return < -0.01:
                score -= 2
                reasons.append(f"Unusual volume ({vol_ratio:.1f}x) on down day ({daily_return:.1%})")

        # Price-volume divergence (price down but volume increasing = accumulation)
        if self.params['price_volume_divergence']:
            price_5d = float((df['Close'].iloc[-1] - df['Close'].iloc[-5]) / df['Close'].iloc[-5])
            vol_5d_avg = float(df['Volume'].iloc[-5:].mean())
            vol_prev_5d_avg = float(df['Volume'].iloc[-10:-5].mean()) if len(df) >= 10 else vol_5d_avg

            if price_5d < -0.02 and vol_5d_avg > vol_prev_5d_avg * 1.3:
                score += 2
                reasons.append(f"Accumulation signal: price {price_5d:.1%} but volume +{((vol_5d_avg/vol_prev_5d_avg)-1):.0%}")
            elif price_5d > 0.02 and vol_5d_avg > vol_prev_5d_avg * 1.3:
                score += 1
                reasons.append("Volume confirming uptrend")

        # Institutional accumulation pattern (consecutive above-avg volume with tight range)
        if self.params['dark_pool_proxy']:
            n = self.params['institutional_accumulation_days']
            if len(df) >= n:
                recent = df.iloc[-n:]
                avg_vol = float(df['Vol_SMA_20'].iloc[-1]) if 'Vol_SMA_20' in df.columns and pd.notna(df['Vol_SMA_20'].iloc[-1]) else float(df['Volume'].mean())
                above_avg_days = sum(1 for _, row in recent.iterrows() if float(row['Volume']) > avg_vol * 1.2)
                range_pct = float((recent['High'].max() - recent['Low'].min()) / recent['Low'].min())

                if above_avg_days >= n - 1 and range_pct < 0.05:
                    score += 2
                    reasons.append(f"Institutional accumulation: {above_avg_days}/{n} above-avg vol days, tight range ({range_pct:.1%})")

        # Implied volatility proxy (ATR expansion/contraction)
        if len(df) >= 20:
            atr_now = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else 0
            atr_20d_ago = float(df['ATR'].iloc[-20]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-20]) else 0
            if atr_20d_ago > 0:
                iv_change = (atr_now - atr_20d_ago) / atr_20d_ago
                if iv_change > 0.30:
                    reasons.append(f"Volatility expanding ({iv_change:.0%})")
                    if score > 0:
                        score += 1
                elif iv_change < -0.20 and score > 0:
                    reasons.append(f"Volatility compressing (pre-move)")
                    score += 1

        signals = []
        if score >= 3:
            confidence = min(0.5 + (score - 3) * 0.1, 0.85)
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.BUY, confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * 3, 2),
                stop_loss=round(price - atr * 1.5, 2),
                position_size_pct=0.04,
                reasoning="; ".join(reasons),
                metadata={'vol_ratio': round(vol_ratio, 2)},
            ))
        elif score <= -3:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.SELL, confidence=0.65,
                strategy_name=self.name, price=price, reasoning="; ".join(reasons),
            ))
        return signals
