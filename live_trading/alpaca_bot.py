"""
Live Trading Bot - Alpaca Markets API Integration.
Supports both paper and live trading modes.

IMPORTANT: Use paper trading first to validate strategies before going live.
Set ALPACA_BASE_URL to https://paper-api.alpaca.markets for paper trading.

Requirements: pip install alpaca-trade-api
"""
import json
import time
from datetime import datetime
from strategies.strategy_manager import StrategyManager
from core.data_engine import fetch_stock_data, calculate_technical_indicators
from core.config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    WATCHLIST, MAX_POSITION_SIZE, DEFAULT_CAPITAL,
)
from core.risk_manager import RiskManager
from core.diary import get_diary


class AlpacaLiveTrader:
    """
    Live trading bot using Alpaca Markets API.
    Modular design allows Claude to adjust strategies mid-session.

    Implements ``BrokerProtocol`` (see live_trading/broker_protocol.py).
    Every trade is pre-validated by the centralized ``RiskManager`` and
    written to ``data/live_diary.jsonl`` for post-mortem debugging.
    """

    name = "alpaca"
    asset_classes = ["equity"]

    def __init__(self, strategy_manager: StrategyManager,
                 api_key: str = None, secret_key: str = None,
                 base_url: str = None, max_daily_trades: int = 10,
                 risk_manager: RiskManager | None = None,
                 initial_capital: float = DEFAULT_CAPITAL):
        self.manager = strategy_manager
        self.api_key = api_key or ALPACA_API_KEY
        self.secret_key = secret_key or ALPACA_SECRET_KEY
        self.base_url = base_url or ALPACA_BASE_URL
        self.max_daily_trades = max_daily_trades
        self.daily_trade_count = 0
        self.trade_log = []
        self._api = None
        self.risk_manager = risk_manager or RiskManager()
        self.initial_capital = initial_capital
        self.diary = get_diary("data/live_diary.jsonl")

    def _init_api(self):
        """Lazy init Alpaca API client."""
        if self._api is None:
            try:
                import alpaca_trade_api as tradeapi
                self._api = tradeapi.REST(
                    self.api_key, self.secret_key,
                    self.base_url, api_version='v2'
                )
            except ImportError:
                print("Install alpaca-trade-api: pip install alpaca-trade-api")
                return False
        return True

    def get_account(self) -> dict:
        """Get account info."""
        if not self._init_api():
            return {'error': 'API not initialized'}
        account = self._api.get_account()
        return {
            'equity': float(account.equity),
            'cash': float(account.cash),
            'buying_power': float(account.buying_power),
            'portfolio_value': float(account.portfolio_value),
            'day_trade_count': int(account.daytrade_count),
            'status': account.status,
        }

    def get_positions(self) -> list[dict]:
        """Get current positions."""
        if not self._init_api():
            return []
        positions = self._api.list_positions()
        return [{
            'ticker': p.symbol,
            'shares': int(p.qty),
            'avg_cost': float(p.avg_entry_price),
            'current_price': float(p.current_price),
            'market_value': float(p.market_value),
            'unrealized_pnl': float(p.unrealized_pl),
            'unrealized_pnl_pct': float(p.unrealized_plpc) * 100,
        } for p in positions]

    def submit_order(self, ticker: str, qty: int, side: str,
                     order_type: str = 'market', limit_price: float = None,
                     stop_price: float = None, time_in_force: str = 'day') -> dict:
        """Submit an order to Alpaca."""
        if not self._init_api():
            return {'error': 'API not initialized'}

        if self.daily_trade_count >= self.max_daily_trades:
            return {'error': f'Daily trade limit reached ({self.max_daily_trades})'}

        # Safety: skip crypto tickers not supported on Alpaca equities
        if ticker in ('ETH',):
            return {'error': f'{ticker} not tradeable via Alpaca equities'}

        try:
            kwargs = {
                'symbol': ticker,
                'qty': qty,
                'side': side,
                'type': order_type,
                'time_in_force': time_in_force,
            }
            if limit_price and order_type in ('limit', 'stop_limit'):
                kwargs['limit_price'] = limit_price
            if stop_price and order_type in ('stop', 'stop_limit'):
                kwargs['stop_price'] = stop_price

            order = self._api.submit_order(**kwargs)
            self.daily_trade_count += 1

            result = {
                'order_id': order.id,
                'ticker': ticker,
                'side': side,
                'qty': qty,
                'type': order_type,
                'status': order.status,
                'submitted_at': str(order.submitted_at),
            }
            self.trade_log.append(result)
            return result

        except Exception as e:
            return {'error': str(e)}

    def close_position(self, ticker: str) -> dict:
        """BrokerProtocol: flatten a position. Market-sells the entire holding."""
        positions = {p['ticker']: p for p in self.get_positions()}
        pos = positions.get(ticker)
        if not pos:
            return {'status': 'no_position', 'ticker': ticker, 'broker': self.name}
        return self.submit_order(ticker, int(abs(pos['shares'])), 'sell')

    def get_candles(self, ticker: str, interval: str = '1d',
                    limit: int = 200) -> list[dict]:
        """BrokerProtocol: fetch OHLCV candles via yfinance (ProxiAlpha default)."""
        period_map = {'1d': '1y', '1h': '1mo', '5m': '5d'}
        period = period_map.get(interval, '1y')
        df = fetch_stock_data(ticker, period=period)
        if df is None or df.empty:
            return []
        df = df.tail(limit)
        candles = []
        for ts, row in df.iterrows():
            candles.append({
                't': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
                'open': float(row.get('Open', 0)),
                'high': float(row.get('High', 0)),
                'low': float(row.get('Low', 0)),
                'close': float(row.get('Close', 0)),
                'volume': float(row.get('Volume', 0)),
            })
        return candles

    def submit_bracket_order(self, ticker: str, qty: int, side: str = 'buy',
                              take_profit: float = None, stop_loss: float = None) -> dict:
        """Submit a bracket order (entry + take profit + stop loss)."""
        if not self._init_api():
            return {'error': 'API not initialized'}

        try:
            order = self._api.submit_order(
                symbol=ticker, qty=qty, side=side,
                type='market', time_in_force='day',
                order_class='bracket',
                take_profit={'limit_price': take_profit} if take_profit else None,
                stop_loss={'stop_price': stop_loss} if stop_loss else None,
            )
            self.daily_trade_count += 1
            return {'order_id': order.id, 'status': order.status}
        except Exception as e:
            return {'error': str(e)}

    def run_scan_and_trade(self, tickers: list = None, auto_execute: bool = False) -> list[dict]:
        """
        Main trading loop: scan watchlist, generate signals, optionally execute.

        Args:
            tickers: List of tickers to scan (defaults to WATCHLIST)
            auto_execute: If True, automatically submit orders. USE WITH CAUTION.

        Returns:
            List of signals/trades
        """
        if tickers is None:
            tickers = [t for t in WATCHLIST if t != 'ETH']  # Exclude crypto

        if not self._init_api():
            return [{'error': 'API not initialized'}]

        account = self.get_account()
        equity = account.get('equity', 0)
        current_positions = {p['ticker']: p for p in self.get_positions()}

        results = []
        for ticker in tickers:
            df = fetch_stock_data(ticker, period="6mo")
            if df is None:
                continue

            df = calculate_technical_indicators(df)

            portfolio_state = {
                'cash': account.get('cash', 0),
                'positions': current_positions,
                'equity': equity,
            }

            consensus = self.manager.get_consensus_signal(ticker, df, portfolio_state)
            signal = consensus['consensus']
            confidence = consensus['confidence']
            price = float(df['Close'].iloc[-1])

            action = None

            if signal in ('BUY', 'STRONG_BUY') and confidence > 0.6 and ticker not in current_positions:
                size_pct = min(consensus.get('position_size_pct', 0.05), MAX_POSITION_SIZE)
                amount = equity * size_pct
                qty = int(amount / price)

                # Centralized risk gate ----------------------------------------
                proposed = {
                    'ticker': ticker,
                    'action': 'buy',
                    'allocation_usd': amount,
                    'current_price': price,
                    'sl_price': consensus.get('stop_loss'),
                    'tp_price': consensus.get('target_price'),
                }
                account_state = {
                    'balance': account.get('cash', 0),
                    'cash': account.get('cash', 0),
                    'total_value': equity,
                    'equity': equity,
                    'positions': list(current_positions.values()),
                }
                allowed, rejection, adjusted = self.risk_manager.validate_trade(
                    proposed, account_state, self.initial_capital
                )
                if not allowed:
                    self.diary.log_trade_rejected(proposed, rejection)
                    results.append({
                        'ticker': ticker, 'signal': signal, 'confidence': confidence,
                        'price': price, 'qty': 0, 'amount': 0,
                        'rejected': rejection,
                    })
                    continue

                amount = float(adjusted.get('allocation_usd', amount))
                qty = int(amount / price) if price > 0 else 0
                stop = adjusted.get('sl_price') or consensus.get('stop_loss')
                target = consensus.get('target_price')

                if qty > 0 and auto_execute:
                    self.diary.log_trade_submitted(self.name, proposed)
                    if target and stop:
                        action = self.submit_bracket_order(ticker, qty, 'buy', target, stop)
                    else:
                        action = self.submit_order(ticker, qty, 'buy')
                    self.diary.log_trade_executed(self.name, proposed, action or {})

                results.append({
                    'ticker': ticker, 'signal': signal, 'confidence': confidence,
                    'price': price, 'qty': qty, 'amount': round(amount, 2),
                    'executed': action, 'target': target, 'stop': stop,
                })

            elif signal in ('SELL', 'STRONG_SELL') and ticker in current_positions:
                qty = current_positions[ticker]['shares']
                if auto_execute:
                    action = self.submit_order(ticker, qty, 'sell')

                results.append({
                    'ticker': ticker, 'signal': signal, 'confidence': confidence,
                    'price': price, 'qty': qty,
                    'pnl': current_positions[ticker].get('unrealized_pnl', 0),
                    'executed': action,
                })

        return results

    def run_loop(self, interval_minutes: int = 60, auto_execute: bool = False):
        """
        Continuous trading loop. Scans at regular intervals.
        Press Ctrl+C to stop.
        """
        print(f"Starting live trading loop (interval: {interval_minutes}min, auto_execute: {auto_execute})")
        print("Press Ctrl+C to stop.\n")

        while True:
            try:
                now = datetime.now()
                # Only trade during market hours (9:30 AM - 4:00 PM ET)
                hour = now.hour
                if 9 <= hour < 16:  # Simplified check
                    print(f"\n[{now.strftime('%H:%M:%S')}] Running scan...")
                    results = self.run_scan_and_trade(auto_execute=auto_execute)
                    for r in results:
                        print(f"  {r['ticker']}: {r['signal']} (conf: {r['confidence']:.0%})")
                else:
                    print(f"[{now.strftime('%H:%M:%S')}] Market closed. Waiting...")

                time.sleep(interval_minutes * 60)

            except KeyboardInterrupt:
                print("\nStopping trading loop.")
                break
            except Exception as e:
                print(f"Error in trading loop: {e}")
                time.sleep(60)
