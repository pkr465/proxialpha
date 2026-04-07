"""
AI Strategy - Plugin interface for Claude / any LLM to provide live trading signals.

This module is designed to be the bridge between the ProxiAlpha platform and Claude AI.
Claude can:
  1. Analyze market data and generate signals via the API
  2. Dynamically create/modify custom rules
  3. Adjust strategy weights and parameters in real-time
  4. Provide natural language reasoning for trades

Usage with Claude API:
    from anthropic import Anthropic
    client = Anthropic()

    ai_strategy = AIStrategy(
        api_key="your-key",
        model="claude-sonnet-4-6"
    )
    strategy_manager.register_strategy(ai_strategy)
"""
import json
import pandas as pd
from datetime import datetime
from strategies.base import BaseStrategy, StrategySignal, SignalType


class AIStrategy(BaseStrategy):
    """
    Strategy that uses an LLM (Claude) to generate trading signals.
    Accepts signals via:
      1. Direct API calls to Claude
      2. Manual signal injection (for testing)
      3. Webhook endpoint (for scheduled tasks)
    """

    def __init__(self, weight=1.0, params=None):
        defaults = {
            'model': 'claude-sonnet-4-6',
            'api_key': None,
            'use_api': False,          # Set True to make live API calls
            'fallback_to_rules': True, # Use rule-based fallback if API fails
            'max_tokens': 1024,
            'system_prompt': self._default_system_prompt(),
        }
        if params:
            defaults.update(params)
        super().__init__("AI_Claude", weight, defaults)
        self._pending_signals = []  # Manually injected signals
        self._client = None

    @staticmethod
    def _default_system_prompt():
        return """You are a quantitative trading analyst. Analyze the provided stock data
and generate precise trading signals. For each stock, respond with a JSON object:
{
    "signal": "BUY" | "SELL" | "HOLD" | "STRONG_BUY" | "STRONG_SELL",
    "confidence": 0.0-1.0,
    "target_price": number or null,
    "stop_loss": number or null,
    "position_size_pct": 0.01-0.10,
    "reasoning": "brief explanation"
}
Consider: technical indicators, pullback depth, volume, sector momentum,
risk/reward ratio, and portfolio diversification."""

    def _init_client(self):
        """Lazy init of Anthropic client."""
        if self._client is None and self.params.get('api_key'):
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=self.params['api_key'])
            except ImportError:
                print("anthropic package not installed. Run: pip install anthropic")

    def inject_signal(self, signal: StrategySignal):
        """Manually inject a signal (for testing or webhook-based updates)."""
        self._pending_signals.append(signal)

    def inject_signals_from_json(self, json_signals: list[dict]):
        """Inject signals from JSON (e.g., Claude API response parsed externally)."""
        signal_map = {
            'BUY': SignalType.BUY, 'SELL': SignalType.SELL,
            'HOLD': SignalType.HOLD, 'STRONG_BUY': SignalType.STRONG_BUY,
            'STRONG_SELL': SignalType.STRONG_SELL,
        }
        for s in json_signals:
            self._pending_signals.append(StrategySignal(
                ticker=s['ticker'],
                signal=signal_map.get(s.get('signal', 'HOLD'), SignalType.HOLD),
                confidence=s.get('confidence', 0.5),
                strategy_name=self.name,
                price=s.get('price', 0),
                target_price=s.get('target_price'),
                stop_loss=s.get('stop_loss'),
                position_size_pct=s.get('position_size_pct', 0.05),
                reasoning=s.get('reasoning', 'AI generated signal'),
            ))

    def _prepare_market_context(self, ticker: str, df: pd.DataFrame) -> str:
        """Format market data as context for Claude."""
        latest = df.tail(5)
        summary = {
            'ticker': ticker,
            'current_price': float(df['Close'].iloc[-1]),
            'rsi': float(df['RSI'].iloc[-1]) if 'RSI' in df.columns and pd.notna(df['RSI'].iloc[-1]) else None,
            'macd_hist': float(df['MACD_Hist'].iloc[-1]) if 'MACD_Hist' in df.columns and pd.notna(df['MACD_Hist'].iloc[-1]) else None,
            'sma20': float(df['SMA_20'].iloc[-1]) if 'SMA_20' in df.columns and pd.notna(df['SMA_20'].iloc[-1]) else None,
            'sma50': float(df['SMA_50'].iloc[-1]) if 'SMA_50' in df.columns and pd.notna(df['SMA_50'].iloc[-1]) else None,
            'vol_ratio': float(df['Vol_Ratio'].iloc[-1]) if 'Vol_Ratio' in df.columns and pd.notna(df['Vol_Ratio'].iloc[-1]) else None,
            'atr': float(df['ATR'].iloc[-1]) if 'ATR' in df.columns and pd.notna(df['ATR'].iloc[-1]) else None,
            'drawdown_pct': float(df['Drawdown_Pct'].iloc[-1]) if 'Drawdown_Pct' in df.columns and pd.notna(df['Drawdown_Pct'].iloc[-1]) else None,
            'recent_prices': [float(x) for x in latest['Close'].values],
            'recent_volumes': [int(x) for x in latest['Volume'].values],
        }
        return json.dumps(summary, indent=2)

    def _call_claude_api(self, ticker: str, context: str) -> StrategySignal | None:
        """Make a live API call to Claude for a trading signal."""
        self._init_client()
        if self._client is None:
            return None

        try:
            response = self._client.messages.create(
                model=self.params['model'],
                max_tokens=self.params['max_tokens'],
                system=self.params['system_prompt'],
                messages=[{
                    "role": "user",
                    "content": f"Analyze this stock and provide a trading signal:\n{context}"
                }],
            )

            # Parse JSON from Claude's response
            text = response.content[0].text
            # Try to extract JSON from the response
            import re
            json_match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                signal_map = {
                    'BUY': SignalType.BUY, 'SELL': SignalType.SELL,
                    'HOLD': SignalType.HOLD, 'STRONG_BUY': SignalType.STRONG_BUY,
                    'STRONG_SELL': SignalType.STRONG_SELL,
                }
                return StrategySignal(
                    ticker=ticker,
                    signal=signal_map.get(data.get('signal', 'HOLD'), SignalType.HOLD),
                    confidence=data.get('confidence', 0.5),
                    strategy_name=self.name,
                    price=data.get('current_price', 0),
                    target_price=data.get('target_price'),
                    stop_loss=data.get('stop_loss'),
                    position_size_pct=data.get('position_size_pct', 0.05),
                    reasoning=data.get('reasoning', text[:200]),
                )
        except Exception as e:
            print(f"Claude API error for {ticker}: {e}")
        return None

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        signals = []

        # Return any manually injected signals for this ticker
        ticker_signals = [s for s in self._pending_signals if s.ticker == ticker]
        signals.extend(ticker_signals)
        self._pending_signals = [s for s in self._pending_signals if s.ticker != ticker]

        # If using live API, call Claude
        if self.params.get('use_api') and self.params.get('api_key'):
            context = self._prepare_market_context(ticker, df)
            api_signal = self._call_claude_api(ticker, context)
            if api_signal:
                signals.append(api_signal)

        return signals


class AIStrategyWebhook:
    """
    Webhook handler for receiving strategy updates from Claude.
    Can be used with a Flask/FastAPI server to receive real-time updates.

    Example FastAPI endpoint:
        @app.post("/ai/signals")
        async def receive_signals(payload: dict):
            webhook = AIStrategyWebhook(ai_strategy)
            webhook.process_payload(payload)
    """

    def __init__(self, ai_strategy: AIStrategy):
        self.strategy = ai_strategy

    def process_payload(self, payload: dict):
        """Process incoming AI signals payload."""
        if 'signals' in payload:
            self.strategy.inject_signals_from_json(payload['signals'])

        if 'rule_updates' in payload:
            # Claude can also update custom rules
            return payload['rule_updates']

        if 'param_updates' in payload:
            self.strategy.update_params(payload['param_updates'])

        return {'status': 'ok', 'signals_queued': len(payload.get('signals', []))}
