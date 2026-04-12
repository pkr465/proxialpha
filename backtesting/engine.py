"""
Backtesting Engine - Test strategies against historical data.
"""
import pandas as pd
import numpy as np
from datetime import datetime
from strategies.base import BaseStrategy, StrategySignal, SignalType
from strategies.strategy_manager import StrategyManager
from core.config import DEFAULT_CAPITAL, MAX_POSITION_SIZE, COMMISSION_PER_TRADE
from core.risk_manager import RiskManager
from core.diary import get_diary


class BacktestEngine:
    """
    Event-driven backtesting engine.
    Walks through historical data day-by-day, generates signals, simulates execution.

    As of the Hyperliquid integration, every proposed fill passes through the
    centralized ``RiskManager`` so backtest behavior matches live trading.
    """

    def __init__(self, strategy_manager: StrategyManager,
                 initial_capital: float = DEFAULT_CAPITAL,
                 risk_manager: RiskManager | None = None,
                 enable_diary: bool = False):
        self.manager = strategy_manager
        self.initial_capital = initial_capital
        self.results = None
        self.risk_manager = risk_manager or RiskManager()
        self.enable_diary = enable_diary
        self._diary = get_diary("data/backtest_diary.jsonl") if enable_diary else None

    def run(self, data: dict[str, pd.DataFrame], start_date: str = None, end_date: str = None) -> dict:
        """
        Run backtest across all tickers.

        Args:
            data: Dict of ticker -> DataFrame with OHLCV + indicators
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
        """
        # Get common date range
        all_dates = set()
        for df in data.values():
            all_dates.update(df.index.strftime('%Y-%m-%d'))
        all_dates = sorted(all_dates)

        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        # Portfolio state
        cash = self.initial_capital
        positions = {}  # ticker -> {shares, avg_cost, entry_date}
        trade_log = []
        equity_curve = []
        daily_returns = []

        prev_equity = self.initial_capital

        for date_str in all_dates:
            date = pd.Timestamp(date_str)
            day_value = cash

            # Update position values and check stops/targets
            for ticker in list(positions.keys()):
                if ticker not in data:
                    continue
                df = data[ticker]
                if date not in df.index:
                    continue

                price = float(df.loc[date, 'Close'])
                pos = positions[ticker]
                day_value += pos['shares'] * price

                # Check stop loss
                if pos.get('stop_loss') and price <= pos['stop_loss']:
                    proceeds = pos['shares'] * price - COMMISSION_PER_TRADE
                    cash += proceeds
                    pnl = proceeds - (pos['shares'] * pos['avg_cost'])
                    trade_log.append({
                        'date': date_str, 'ticker': ticker, 'action': 'STOP_LOSS',
                        'shares': pos['shares'], 'price': price, 'pnl': round(pnl, 2),
                    })
                    del positions[ticker]
                    continue

                # Check take profit
                if pos.get('target') and price >= pos['target']:
                    proceeds = pos['shares'] * price - COMMISSION_PER_TRADE
                    cash += proceeds
                    pnl = proceeds - (pos['shares'] * pos['avg_cost'])
                    trade_log.append({
                        'date': date_str, 'ticker': ticker, 'action': 'TAKE_PROFIT',
                        'shares': pos['shares'], 'price': price, 'pnl': round(pnl, 2),
                    })
                    del positions[ticker]
                    continue

            # Generate signals for each ticker
            portfolio_state = {'cash': cash, 'positions': positions, 'equity': day_value}

            for ticker, df in data.items():
                if date not in df.index:
                    continue

                # Get data up to current date (no look-ahead)
                hist = df.loc[:date]
                if len(hist) < 50:
                    continue

                consensus = self.manager.get_consensus_signal(ticker, hist, portfolio_state)
                signal = consensus['consensus']
                confidence = consensus['confidence']

                # Execute BUY
                if signal in ('BUY', 'STRONG_BUY') and ticker not in positions and confidence > 0.5:
                    price = float(df.loc[date, 'Close'])
                    size_pct = min(consensus['position_size_pct'], MAX_POSITION_SIZE)
                    allocation = day_value * size_pct
                    allocation = min(allocation, cash * 0.95)  # Keep 5% cash buffer

                    # Centralized risk gate -------------------------------------------------
                    proposed = {
                        'ticker': ticker,
                        'action': 'buy',
                        'allocation_usd': allocation,
                        'current_price': price,
                        'sl_price': consensus.get('stop_loss'),
                        'tp_price': consensus.get('target_price'),
                    }
                    account_state = {
                        'balance': cash,
                        'cash': cash,
                        'total_value': day_value,
                        'equity': day_value,
                        'positions': positions,
                    }
                    allowed, reason, adjusted = self.risk_manager.validate_trade(
                        proposed, account_state, self.initial_capital
                    )
                    if not allowed:
                        if self._diary:
                            self._diary.log_trade_rejected(proposed, reason)
                        continue

                    allocation = float(adjusted.get('allocation_usd', allocation))
                    sl_price = adjusted.get('sl_price') or consensus.get('stop_loss')

                    if allocation > 100:  # Minimum $100 position
                        shares = int(allocation / price)
                        if shares > 0:
                            cost = shares * price + COMMISSION_PER_TRADE
                            if cost > cash:
                                continue
                            cash -= cost
                            positions[ticker] = {
                                'shares': shares,
                                'avg_cost': price,
                                'entry_date': date_str,
                                'stop_loss': sl_price,
                                'target': consensus.get('target_price'),
                            }
                            trade_log.append({
                                'date': date_str, 'ticker': ticker, 'action': 'BUY',
                                'shares': shares, 'price': price, 'cost': round(cost, 2),
                            })
                            if self._diary:
                                self._diary.log_trade_executed(
                                    'backtest', proposed,
                                    {'shares': shares, 'cost': cost, 'sl': sl_price},
                                )

                # Execute SELL
                elif signal in ('SELL', 'STRONG_SELL') and ticker in positions:
                    price = float(df.loc[date, 'Close'])
                    pos = positions[ticker]
                    proceeds = pos['shares'] * price - COMMISSION_PER_TRADE
                    pnl = proceeds - (pos['shares'] * pos['avg_cost'])
                    cash += proceeds
                    trade_log.append({
                        'date': date_str, 'ticker': ticker, 'action': 'SELL',
                        'shares': pos['shares'], 'price': price, 'pnl': round(pnl, 2),
                    })
                    del positions[ticker]

            # Record daily equity
            total_equity = cash
            for ticker, pos in positions.items():
                if ticker in data and date in data[ticker].index:
                    total_equity += pos['shares'] * float(data[ticker].loc[date, 'Close'])

            daily_return = (total_equity - prev_equity) / prev_equity if prev_equity > 0 else 0
            daily_returns.append(daily_return)
            prev_equity = total_equity

            equity_curve.append({
                'date': date_str,
                'equity': round(total_equity, 2),
                'cash': round(cash, 2),
                'positions_value': round(total_equity - cash, 2),
                'num_positions': len(positions),
            })

        self.results = {
            'equity_curve': pd.DataFrame(equity_curve),
            'trade_log': pd.DataFrame(trade_log) if trade_log else pd.DataFrame(),
            'daily_returns': daily_returns,
            'final_equity': equity_curve[-1]['equity'] if equity_curve else self.initial_capital,
            'final_positions': positions,
        }
        return self.results

    def get_summary(self) -> dict:
        """Get backtest performance summary."""
        if not self.results:
            return {}
        return calculate_metrics(
            self.results['equity_curve'],
            self.results['trade_log'],
            self.results['daily_returns'],
            self.initial_capital,
        )


def calculate_metrics(equity_df, trades_df, daily_returns, initial_capital):
    """Calculate comprehensive backtest metrics."""
    if equity_df.empty:
        return {}

    final = equity_df['equity'].iloc[-1]
    total_return = (final - initial_capital) / initial_capital
    days = len(equity_df)
    annual_return = (1 + total_return) ** (252 / max(days, 1)) - 1

    returns = np.array(daily_returns)
    sharpe = np.sqrt(252) * returns.mean() / returns.std() if returns.std() > 0 else 0
    downside = returns[returns < 0]
    sortino = np.sqrt(252) * returns.mean() / downside.std() if len(downside) > 0 and downside.std() > 0 else 0

    # Max drawdown
    equity = equity_df['equity'].values
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = drawdown.min()

    # Trade stats
    num_trades = len(trades_df) if not trades_df.empty else 0
    if not trades_df.empty and 'pnl' in trades_df.columns:
        pnl_trades = trades_df[trades_df['pnl'].notna()]
        wins = pnl_trades[pnl_trades['pnl'] > 0]
        losses = pnl_trades[pnl_trades['pnl'] <= 0]
        win_rate = len(wins) / len(pnl_trades) if len(pnl_trades) > 0 else 0
        avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
        avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
        profit_factor = abs(wins['pnl'].sum() / losses['pnl'].sum()) if len(losses) > 0 and losses['pnl'].sum() != 0 else float('inf')
    else:
        win_rate = avg_win = avg_loss = profit_factor = 0

    return {
        'initial_capital': initial_capital,
        'final_equity': round(final, 2),
        'total_return_pct': round(total_return * 100, 2),
        'annual_return_pct': round(annual_return * 100, 2),
        'sharpe_ratio': round(sharpe, 2),
        'sortino_ratio': round(sortino, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'total_trades': num_trades,
        'win_rate_pct': round(win_rate * 100, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'trading_days': days,
    }
