"""
Dip Buyer Strategy - Buy at fixed pullback levels, sell at recovery targets.
"""
import pandas as pd
from strategies.base import BaseStrategy, StrategySignal, SignalType
from core.config import WATCHLIST, DIP_BUY_LEVELS, RECOVERY_TARGETS, STOP_LOSS_PCT


class DipBuyerStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'dip_levels': DIP_BUY_LEVELS,       # Buy when drawdown hits these levels
            'recovery_targets': RECOVERY_TARGETS, # Sell when recovery hits these levels
            'stop_loss_pct': STOP_LOSS_PCT,
            'min_volume_ratio': 0.8,             # Minimum volume vs 20d avg
        }
        if params:
            defaults.update(params)
        super().__init__("DipBuyer", weight, defaults)

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if ticker not in WATCHLIST or df.empty:
            return []

        signals = []
        info = WATCHLIST[ticker]
        current_price = float(df['Close'].iloc[-1])
        high = info['high']
        low = info['low']
        price_range = high - low

        if price_range <= 0:
            return []

        drawdown_pct = (high - current_price) / high
        recovery_pct = (current_price - low) / price_range

        vol_ratio = float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else 1.0

        # Check if price is at a dip buy level
        for level in self.params['dip_levels']:
            if drawdown_pct >= level and vol_ratio >= self.params['min_volume_ratio']:
                # Deeper dip = higher confidence and larger position
                confidence = min(0.5 + (drawdown_pct - level) * 2, 0.95)
                position_size = 0.03 + (drawdown_pct * 0.07)  # 3-10% based on depth

                # Find the nearest recovery target
                nearest_target = None
                for target in self.params['recovery_targets']:
                    target_price = low + (price_range * target)
                    if target_price > current_price:
                        nearest_target = target_price
                        break

                stop = current_price * (1 - self.params['stop_loss_pct'])

                signals.append(StrategySignal(
                    ticker=ticker,
                    signal=SignalType.STRONG_BUY if drawdown_pct > 0.60 else SignalType.BUY,
                    confidence=round(confidence, 2),
                    strategy_name=self.name,
                    price=current_price,
                    target_price=round(nearest_target, 2) if nearest_target else None,
                    stop_loss=round(stop, 2),
                    position_size_pct=round(position_size, 3),
                    reasoning=f"Pullback of {drawdown_pct:.0%} from ATH ${high}. "
                              f"Recovery at {recovery_pct:.0%}. "
                              f"Vol ratio: {vol_ratio:.1f}x. "
                              f"Target: ${nearest_target:.0f}" if nearest_target else "",
                ))
                break  # One signal per ticker

        # Check sell signals if we hold the position
        if portfolio and ticker in portfolio.get('positions', {}):
            for target in reversed(self.params['recovery_targets']):
                if recovery_pct >= target:
                    signals.append(StrategySignal(
                        ticker=ticker,
                        signal=SignalType.SELL,
                        confidence=0.7 + (target * 0.2),
                        strategy_name=self.name,
                        price=current_price,
                        reasoning=f"Recovery hit {recovery_pct:.0%} (target: {target:.0%})",
                    ))
                    break

        return signals
