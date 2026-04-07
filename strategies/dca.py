"""
Dollar-Cost Averaging Strategy - Accumulate positions in pulled-back stocks over time.
"""
import pandas as pd
from datetime import datetime
from strategies.base import BaseStrategy, StrategySignal, SignalType
from core.config import WATCHLIST, DCA_INTERVALS


class DCAStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {
            'num_tranches': DCA_INTERVALS,
            'min_drawdown_to_start': 0.30,  # Start DCA when 30%+ drawdown
            'tranche_interval_days': 7,     # Buy every 7 days
            'tranche_size_pct': 0.02,       # 2% of portfolio per tranche
            'scale_with_drawdown': True,     # Larger buys at deeper drawdowns
        }
        if params:
            defaults.update(params)
        super().__init__("DCA", weight, defaults)
        self.dca_state = {}  # Track DCA progress per ticker

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if ticker not in WATCHLIST or df.empty:
            return []

        signals = []
        info = WATCHLIST[ticker]
        current_price = float(df['Close'].iloc[-1])
        high = info['high']
        low = info['low']
        drawdown_pct = (high - current_price) / high

        if drawdown_pct < self.params['min_drawdown_to_start']:
            return []

        # Initialize DCA state
        if ticker not in self.dca_state:
            self.dca_state[ticker] = {
                'tranches_bought': 0,
                'last_buy_date': None,
                'avg_cost': 0,
                'total_invested': 0,
            }

        state = self.dca_state[ticker]
        today = datetime.now()

        # Check if we should buy another tranche
        can_buy = state['tranches_bought'] < self.params['num_tranches']
        interval_ok = (
            state['last_buy_date'] is None or
            (today - state['last_buy_date']).days >= self.params['tranche_interval_days']
        )

        if can_buy and interval_ok:
            # Scale position size with drawdown depth
            base_size = self.params['tranche_size_pct']
            if self.params['scale_with_drawdown']:
                scale_factor = 1 + (drawdown_pct - self.params['min_drawdown_to_start'])
                size = base_size * scale_factor
            else:
                size = base_size

            tranche_num = state['tranches_bought'] + 1
            confidence = 0.6 + (drawdown_pct * 0.3)  # More confident at deeper drawdowns

            signals.append(StrategySignal(
                ticker=ticker,
                signal=SignalType.BUY,
                confidence=round(min(confidence, 0.9), 2),
                strategy_name=self.name,
                price=current_price,
                target_price=round(high * 0.75, 2),  # Target 75% of ATH
                position_size_pct=round(size, 3),
                reasoning=f"DCA tranche {tranche_num}/{self.params['num_tranches']}. "
                          f"Drawdown: {drawdown_pct:.0%}. "
                          f"Avg cost: ${state['avg_cost']:.2f}" if state['avg_cost'] > 0 else
                          f"DCA tranche {tranche_num}/{self.params['num_tranches']}. "
                          f"Drawdown: {drawdown_pct:.0%}. Starting DCA.",
                metadata={
                    'tranche': tranche_num,
                    'total_tranches': self.params['num_tranches'],
                    'drawdown': round(drawdown_pct, 3),
                },
            ))

        return signals

    def record_execution(self, ticker: str, price: float, amount: float):
        """Call after a DCA buy is executed to update state."""
        if ticker not in self.dca_state:
            self.dca_state[ticker] = {'tranches_bought': 0, 'last_buy_date': None, 'avg_cost': 0, 'total_invested': 0}

        state = self.dca_state[ticker]
        old_total = state['total_invested']
        state['total_invested'] += amount
        state['tranches_bought'] += 1
        state['last_buy_date'] = datetime.now()

        if state['total_invested'] > 0:
            shares_before = old_total / state['avg_cost'] if state['avg_cost'] > 0 else 0
            new_shares = amount / price
            total_shares = shares_before + new_shares
            state['avg_cost'] = state['total_invested'] / total_shares if total_shares > 0 else price
