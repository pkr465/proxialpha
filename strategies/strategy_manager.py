"""
Strategy Manager - Orchestrates all strategies, combines signals, manages execution.
This is the central hub that Claude AI can control to adjust the trading system.
"""
import json
import pandas as pd
from datetime import datetime
from strategies.base import BaseStrategy, StrategySignal, SignalType


class StrategyManager:
    """
    Central orchestrator for all trading strategies.

    Plugin Architecture:
    - Register/unregister strategies at runtime
    - Adjust weights dynamically (Claude can rebalance strategies)
    - Combine signals from multiple strategies with weighted voting
    - Export/import strategy configurations as JSON
    """

    def __init__(self):
        self.strategies: dict[str, BaseStrategy] = {}
        self.signal_history: list[dict] = []

    def register_strategy(self, strategy: BaseStrategy):
        """Add a strategy to the manager."""
        self.strategies[strategy.name] = strategy

    def unregister_strategy(self, name: str):
        """Remove a strategy."""
        self.strategies.pop(name, None)

    def set_weights(self, weights: dict[str, float]):
        """Update strategy weights (e.g., from Claude AI recommendation)."""
        for name, weight in weights.items():
            if name in self.strategies:
                self.strategies[name].weight = weight

    def get_all_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        """Get signals from all active strategies for a ticker."""
        all_signals = []
        for strategy in self.strategies.values():
            if strategy.is_active:
                try:
                    signals = strategy.generate_signals(ticker, df, portfolio)
                    all_signals.extend(signals)
                except Exception as e:
                    print(f"Error in {strategy.name} for {ticker}: {e}")
        return all_signals

    def get_consensus_signal(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> dict:
        """
        Combine signals from all strategies using weighted voting.
        Returns a consensus with aggregated confidence.
        """
        all_signals = self.get_all_signals(ticker, df, portfolio)
        if not all_signals:
            return {'ticker': ticker, 'consensus': 'HOLD', 'confidence': 0, 'signals': []}

        # Weighted scoring
        score_map = {
            SignalType.STRONG_BUY: 2, SignalType.BUY: 1,
            SignalType.HOLD: 0,
            SignalType.SELL: -1, SignalType.STRONG_SELL: -2,
        }

        total_weight = 0
        weighted_score = 0
        weighted_confidence = 0

        for signal in all_signals:
            strat = self.strategies.get(signal.strategy_name.split(':')[0])
            weight = strat.weight if strat else 1.0
            total_weight += weight
            weighted_score += score_map.get(signal.signal, 0) * weight * signal.confidence
            weighted_confidence += signal.confidence * weight

        if total_weight > 0:
            avg_score = weighted_score / total_weight
            avg_confidence = weighted_confidence / total_weight
        else:
            avg_score = 0
            avg_confidence = 0

        # Determine consensus
        if avg_score >= 1.5:
            consensus = 'STRONG_BUY'
        elif avg_score >= 0.5:
            consensus = 'BUY'
        elif avg_score <= -1.5:
            consensus = 'STRONG_SELL'
        elif avg_score <= -0.5:
            consensus = 'SELL'
        else:
            consensus = 'HOLD'

        # Find best target and stop
        buy_signals = [s for s in all_signals if s.signal in (SignalType.BUY, SignalType.STRONG_BUY)]
        best_target = max([s.target_price for s in buy_signals if s.target_price], default=None)
        best_stop = max([s.stop_loss for s in buy_signals if s.stop_loss], default=None)
        best_size = max([s.position_size_pct for s in buy_signals], default=0.05) if buy_signals else 0

        result = {
            'ticker': ticker,
            'consensus': consensus,
            'score': round(avg_score, 2),
            'confidence': round(avg_confidence, 2),
            'target_price': best_target,
            'stop_loss': best_stop,
            'position_size_pct': round(best_size, 3),
            'num_strategies': len(all_signals),
            'signals': [s.to_dict() for s in all_signals],
            'timestamp': datetime.now().isoformat(),
        }

        self.signal_history.append(result)
        return result

    def scan_all_tickers(self, data: dict[str, pd.DataFrame], portfolio: dict = None) -> pd.DataFrame:
        """Run all strategies across all tickers and return a summary."""
        results = []
        for ticker, df in data.items():
            consensus = self.get_consensus_signal(ticker, df, portfolio)
            results.append({
                'Ticker': consensus['ticker'],
                'Signal': consensus['consensus'],
                'Score': consensus['score'],
                'Confidence': consensus['confidence'],
                'Target': consensus['target_price'],
                'Stop': consensus['stop_loss'],
                'Size %': consensus['position_size_pct'],
                '# Strategies': consensus['num_strategies'],
            })

        return pd.DataFrame(results).sort_values('Score', ascending=False)

    def export_config(self) -> str:
        """Export current configuration as JSON (for saving/sharing with Claude)."""
        config = {}
        for name, strat in self.strategies.items():
            config[name] = {
                'weight': strat.weight,
                'active': strat.is_active,
                'params': strat.params,
            }
        return json.dumps(config, indent=2, default=str)

    def import_config(self, config_json: str):
        """Import configuration from JSON (e.g., from Claude AI recommendations)."""
        config = json.loads(config_json)
        for name, settings in config.items():
            if name in self.strategies:
                self.strategies[name].weight = settings.get('weight', 1.0)
                self.strategies[name].is_active = settings.get('active', True)
                if 'params' in settings:
                    self.strategies[name].update_params(settings['params'])
