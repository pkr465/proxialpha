"""
Trend Following Strategy - Identifies and rides established trends using
multiple timeframe moving averages, ADX, and price structure.
"""
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, StrategySignal, SignalType


class TrendFollowingStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'ema_fast': 9,
            'ema_slow': 21,
            'sma_trend': 50,
            'sma_macro': 200,
            'adx_threshold': 25,
            'adx_period': 14,
            'higher_highs_lookback': 10,
            'stop_loss_atr_mult': 2.0,
        }
        if params:
            defaults.update(params)
        super().__init__("TrendFollowing", weight, defaults)

    def _calculate_adx(self, df, period=14):
        high, low, close = df['High'], df['Low'], df['Close']
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        mask = plus_dm < minus_dm
        plus_dm[mask] = 0
        minus_dm[~mask] = 0

        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
        adx = dx.rolling(period).mean()
        return adx, plus_di, minus_di

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty or len(df) < self.params['sma_macro'] + 20:
            return []

        close = df['Close']
        price = float(close.iloc[-1])

        ema_f = close.ewm(span=self.params['ema_fast']).mean()
        ema_s = close.ewm(span=self.params['ema_slow']).mean()
        sma_t = close.rolling(self.params['sma_trend']).mean()
        sma_m = close.rolling(self.params['sma_macro']).mean()

        ef = float(ema_f.iloc[-1])
        es = float(ema_s.iloc[-1])
        st = float(sma_t.iloc[-1])
        sm = float(sma_m.iloc[-1])

        adx, plus_di, minus_di = self._calculate_adx(df, self.params['adx_period'])
        adx_val = float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else 0
        pdi = float(plus_di.iloc[-1]) if pd.notna(plus_di.iloc[-1]) else 0
        mdi = float(minus_di.iloc[-1]) if pd.notna(minus_di.iloc[-1]) else 0

        # EMA crossover
        prev_ef = float(ema_f.iloc[-2])
        prev_es = float(ema_s.iloc[-2])

        # Higher highs / higher lows check
        lb = self.params['higher_highs_lookback']
        recent_highs = df['High'].iloc[-lb:]
        recent_lows = df['Low'].iloc[-lb:]
        mid = lb // 2
        hh = float(recent_highs.iloc[mid:].max()) > float(recent_highs.iloc[:mid].max())
        hl = float(recent_lows.iloc[mid:].min()) > float(recent_lows.iloc[:mid].min())

        score = 0
        reasons = []

        # Moving average alignment
        if ef > es > st > sm:
            score += 3
            reasons.append("Full bullish MA alignment (EMA9 > EMA21 > SMA50 > SMA200)")
        elif ef < es < st < sm:
            score -= 3
            reasons.append("Full bearish MA alignment")
        elif price > st and ef > es:
            score += 1
            reasons.append("Above SMA50 with bullish EMA cross")
        elif price < st and ef < es:
            score -= 1

        # EMA crossover
        if prev_ef <= prev_es and ef > es:
            score += 2
            reasons.append("Bullish EMA crossover")
        elif prev_ef >= prev_es and ef < es:
            score -= 2
            reasons.append("Bearish EMA crossover")

        # ADX trend strength
        if adx_val > self.params['adx_threshold']:
            if pdi > mdi:
                score += 2
                reasons.append(f"Strong uptrend (ADX {adx_val:.0f}, +DI > -DI)")
            else:
                score -= 2
                reasons.append(f"Strong downtrend (ADX {adx_val:.0f}, -DI > +DI)")

        # Price structure
        if hh and hl:
            score += 1
            reasons.append("Higher highs + higher lows")
        elif not hh and not hl:
            score -= 1

        atr = float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else price * 0.02

        signals = []
        if score >= 4:
            confidence = min(0.5 + (score - 4) * 0.08, 0.95)
            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.STRONG_BUY if score >= 7 else SignalType.BUY,
                confidence=round(confidence, 2),
                strategy_name=self.name, price=price,
                target_price=round(price + atr * 4, 2),
                stop_loss=round(price - atr * self.params['stop_loss_atr_mult'], 2),
                position_size_pct=round(0.04 + confidence * 0.04, 3),
                reasoning="; ".join(reasons),
                metadata={'adx': round(adx_val, 1), 'ema_fast': round(ef, 2), 'ema_slow': round(es, 2)},
            ))
        elif score <= -4:
            signals.append(StrategySignal(
                ticker=ticker, signal=SignalType.STRONG_SELL if score <= -7 else SignalType.SELL,
                confidence=0.7, strategy_name=self.name, price=price,
                reasoning="; ".join(reasons),
            ))

        return signals
