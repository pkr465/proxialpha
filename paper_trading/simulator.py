"""
Paper Trading Simulator - Simulates live trading with real-time data but no real money.
Tracks P&L, positions, and performance metrics in real-time.
"""
import json
import pandas as pd
from datetime import datetime
from pathlib import Path
from strategies.strategy_manager import StrategyManager
from core.data_engine import fetch_stock_data, calculate_technical_indicators
from core.config import DEFAULT_CAPITAL, MAX_POSITION_SIZE, COMMISSION_PER_TRADE, WATCHLIST
from core.risk_manager import RiskManager
from core.diary import get_diary


class PaperTrader:
    """
    Real-time paper trading simulator.
    Fetches live prices, runs strategies, and simulates trades.
    State is persisted to JSON so you can resume sessions.

    Every ``execute_buy`` / ``execute_sell`` call passes through the
    centralized ``RiskManager``, and every action is written to the JSONL
    diary at ``data/paper_diary.jsonl``.
    """

    def __init__(self, strategy_manager: StrategyManager, initial_capital: float = DEFAULT_CAPITAL,
                 state_file: str = "data/paper_trading_state.json",
                 risk_manager: RiskManager | None = None):
        self.manager = strategy_manager
        self.initial_capital = initial_capital
        self.state_file = state_file
        self.state = self._load_state()
        self.risk_manager = risk_manager or RiskManager()
        self.diary = get_diary("data/paper_diary.jsonl")

    def _default_state(self):
        return {
            'cash': self.initial_capital,
            'positions': {},
            'trade_history': [],
            'equity_history': [],
            'start_date': datetime.now().isoformat(),
            'last_update': None,
        }

    def _load_state(self):
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return self._default_state()

    def save_state(self):
        Path(self.state_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2, default=str)

    def get_portfolio_value(self, current_prices: dict = None) -> float:
        """Calculate total portfolio value."""
        value = self.state['cash']
        for ticker, pos in self.state['positions'].items():
            if current_prices and ticker in current_prices:
                price = current_prices[ticker]
            else:
                price = pos.get('last_price', pos['avg_cost'])
            value += pos['shares'] * price
        return value

    def execute_buy(self, ticker: str, price: float, shares: int = None,
                    dollar_amount: float = None, stop_loss: float = None,
                    target: float = None, reason: str = ""):
        """Execute a paper buy order."""
        if dollar_amount and not shares:
            shares = int(dollar_amount / price)
        if not shares or shares <= 0:
            return {'error': 'Invalid share count'}

        allocation_usd = shares * price
        equity = self.get_portfolio_value()

        # Centralized risk gate ----------------------------------------------------
        proposed = {
            'ticker': ticker,
            'action': 'buy',
            'allocation_usd': allocation_usd,
            'current_price': price,
            'sl_price': stop_loss,
            'tp_price': target,
        }
        account_state = {
            'balance': self.state['cash'],
            'cash': self.state['cash'],
            'total_value': equity,
            'equity': equity,
            'positions': self.state['positions'],
        }
        allowed, rejection, adjusted = self.risk_manager.validate_trade(
            proposed, account_state, self.initial_capital
        )
        if not allowed:
            self.diary.log_trade_rejected(proposed, rejection)
            return {'error': f'Risk check rejected: {rejection}'}

        # Risk manager may have capped allocation or auto-set SL.
        allocation_usd = float(adjusted.get('allocation_usd', allocation_usd))
        stop_loss = adjusted.get('sl_price') or stop_loss
        shares = int(allocation_usd / price) if price > 0 else shares
        if shares <= 0:
            return {'error': 'Allocation too small after risk capping'}

        cost = shares * price + COMMISSION_PER_TRADE
        if cost > self.state['cash']:
            return {'error': f'Insufficient cash. Need ${cost:.2f}, have ${self.state["cash"]:.2f}'}

        self.state['cash'] -= cost

        if ticker in self.state['positions']:
            pos = self.state['positions'][ticker]
            total_shares = pos['shares'] + shares
            pos['avg_cost'] = ((pos['avg_cost'] * pos['shares']) + (price * shares)) / total_shares
            pos['shares'] = total_shares
            pos['last_price'] = price
        else:
            self.state['positions'][ticker] = {
                'shares': shares,
                'avg_cost': price,
                'entry_date': datetime.now().isoformat(),
                'last_price': price,
                'stop_loss': stop_loss,
                'target': target,
            }

        trade = {
            'date': datetime.now().isoformat(),
            'ticker': ticker,
            'action': 'BUY',
            'shares': shares,
            'price': price,
            'cost': round(cost, 2),
            'reason': reason,
        }
        self.state['trade_history'].append(trade)
        self.diary.log_trade_executed('paper', proposed, trade)
        self.save_state()
        return trade

    def execute_sell(self, ticker: str, price: float, shares: int = None, reason: str = ""):
        """Execute a paper sell order."""
        if ticker not in self.state['positions']:
            return {'error': f'No position in {ticker}'}

        pos = self.state['positions'][ticker]
        if not shares:
            shares = pos['shares']
        shares = min(shares, pos['shares'])

        proceeds = shares * price - COMMISSION_PER_TRADE
        pnl = (price - pos['avg_cost']) * shares
        self.state['cash'] += proceeds

        if shares >= pos['shares']:
            del self.state['positions'][ticker]
        else:
            pos['shares'] -= shares

        trade = {
            'date': datetime.now().isoformat(),
            'ticker': ticker,
            'action': 'SELL',
            'shares': shares,
            'price': price,
            'proceeds': round(proceeds, 2),
            'pnl': round(pnl, 2),
            'return_pct': round((pnl / (pos['avg_cost'] * shares)) * 100, 2),
            'reason': reason,
        }
        self.state['trade_history'].append(trade)
        self.diary.log_trade_executed('paper', {'ticker': ticker, 'action': 'sell'}, trade)
        self.save_state()
        return trade

    def run_scan(self, tickers: list = None) -> list[dict]:
        """
        Scan watchlist with current prices and generate signals.
        This is the main loop you'd call periodically (e.g., daily).
        """
        if tickers is None:
            tickers = list(WATCHLIST.keys())

        results = []
        current_prices = {}

        for ticker in tickers:
            df = fetch_stock_data(ticker, period="6mo")
            if df is None:
                continue

            df = calculate_technical_indicators(df)
            price = float(df['Close'].iloc[-1])
            current_prices[ticker] = price

            portfolio_state = {
                'cash': self.state['cash'],
                'positions': self.state['positions'],
            }

            consensus = self.manager.get_consensus_signal(ticker, df, portfolio_state)
            consensus['current_price'] = price
            results.append(consensus)

            # Update position prices
            if ticker in self.state['positions']:
                self.state['positions'][ticker]['last_price'] = price

        # Record equity snapshot
        equity = self.get_portfolio_value(current_prices)
        self.state['equity_history'].append({
            'date': datetime.now().isoformat(),
            'equity': round(equity, 2),
            'cash': round(self.state['cash'], 2),
        })
        self.state['last_update'] = datetime.now().isoformat()
        self.save_state()

        return results

    def auto_execute(self, signals: list[dict], require_confirmation: bool = True) -> list[dict]:
        """
        Automatically execute trades based on signals.
        If require_confirmation=True, returns proposed trades for user approval.
        """
        proposed = []
        for signal in signals:
            ticker = signal['ticker']
            action = signal['consensus']
            confidence = signal['confidence']
            price = signal.get('current_price', 0)

            if action in ('BUY', 'STRONG_BUY') and confidence > 0.5 and ticker not in self.state['positions']:
                equity = self.get_portfolio_value()
                size_pct = min(signal.get('position_size_pct', 0.05), MAX_POSITION_SIZE)
                amount = equity * size_pct
                shares = int(amount / price) if price > 0 else 0

                if shares > 0 and amount <= self.state['cash']:
                    proposed.append({
                        'action': 'BUY', 'ticker': ticker, 'shares': shares,
                        'price': price, 'amount': round(amount, 2),
                        'stop_loss': signal.get('stop_loss'),
                        'target': signal.get('target_price'),
                        'confidence': confidence,
                        'reasoning': [s.get('reasoning', '') for s in signal.get('signals', [])],
                    })

            elif action in ('SELL', 'STRONG_SELL') and ticker in self.state['positions']:
                pos = self.state['positions'][ticker]
                proposed.append({
                    'action': 'SELL', 'ticker': ticker, 'shares': pos['shares'],
                    'price': price, 'pnl_est': round((price - pos['avg_cost']) * pos['shares'], 2),
                    'confidence': confidence,
                })

        if not require_confirmation:
            executed = []
            for trade in proposed:
                if trade['action'] == 'BUY':
                    result = self.execute_buy(
                        trade['ticker'], trade['price'], trade['shares'],
                        stop_loss=trade.get('stop_loss'), target=trade.get('target'),
                    )
                else:
                    result = self.execute_sell(trade['ticker'], trade['price'], trade['shares'])
                executed.append(result)
            return executed

        return proposed

    def get_performance(self) -> dict:
        """Get current performance metrics."""
        equity_history = self.state.get('equity_history', [])
        if not equity_history:
            return {}

        current_equity = equity_history[-1]['equity']
        total_return = (current_equity - self.initial_capital) / self.initial_capital

        trades = self.state.get('trade_history', [])
        sells = [t for t in trades if t['action'] == 'SELL' and 'pnl' in t]
        wins = [t for t in sells if t['pnl'] > 0]
        win_rate = len(wins) / len(sells) if sells else 0

        return {
            'equity': round(current_equity, 2),
            'total_return_pct': round(total_return * 100, 2),
            'cash': round(self.state['cash'], 2),
            'num_positions': len(self.state['positions']),
            'total_trades': len(trades),
            'win_rate_pct': round(win_rate * 100, 1),
            'positions': {
                t: {
                    'shares': p['shares'],
                    'avg_cost': p['avg_cost'],
                    'last_price': p.get('last_price', p['avg_cost']),
                    'unrealized_pnl': round((p.get('last_price', p['avg_cost']) - p['avg_cost']) * p['shares'], 2),
                }
                for t, p in self.state['positions'].items()
            },
        }

    def reset(self):
        """Reset paper trading state."""
        self.state = self._default_state()
        self.save_state()
