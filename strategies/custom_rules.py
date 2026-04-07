"""
Custom Rules Strategy - User-definable rule engine.
Rules are JSON-serializable so Claude AI can generate and inject them at runtime.
"""
import pandas as pd
from strategies.base import BaseStrategy, StrategySignal, SignalType


# Example rule format that Claude can generate:
EXAMPLE_RULES = [
    {
        "name": "Oversold Bounce",
        "conditions": [
            {"indicator": "RSI", "operator": "<", "value": 30},
            {"indicator": "MACD_Hist", "operator": ">", "value": 0},
            {"indicator": "Vol_Ratio", "operator": ">", "value": 1.2},
        ],
        "action": "BUY",
        "confidence": 0.75,
        "position_size": 0.05,
        "stop_loss_pct": 0.08,
        "target_pct": 0.20,
    },
    {
        "name": "Death Cross Exit",
        "conditions": [
            {"indicator": "SMA_20", "operator": "<", "value_ref": "SMA_50"},
            {"indicator": "RSI", "operator": ">", "value": 50},
        ],
        "action": "SELL",
        "confidence": 0.70,
    },
]


class CustomRulesStrategy(BaseStrategy):
    def __init__(self, weight=1.0, params=None):
        defaults = {'rules': EXAMPLE_RULES}
        if params:
            defaults.update(params)
        super().__init__("CustomRules", weight, defaults)

    def add_rule(self, rule: dict):
        """Add a new rule dynamically (e.g., from Claude AI)."""
        self.params['rules'].append(rule)

    def remove_rule(self, rule_name: str):
        """Remove a rule by name."""
        self.params['rules'] = [r for r in self.params['rules'] if r['name'] != rule_name]

    def replace_rules(self, rules: list[dict]):
        """Replace all rules (e.g., when Claude generates a complete new ruleset)."""
        self.params['rules'] = rules

    def _evaluate_condition(self, condition: dict, latest: pd.Series) -> bool:
        """Evaluate a single rule condition against current data."""
        indicator = condition['indicator']
        operator = condition['operator']

        if indicator not in latest.index or pd.isna(latest[indicator]):
            return False

        actual_value = float(latest[indicator])

        # Compare against a fixed value or another indicator
        if 'value_ref' in condition:
            ref = condition['value_ref']
            if ref not in latest.index or pd.isna(latest[ref]):
                return False
            compare_value = float(latest[ref])
        else:
            compare_value = condition['value']

        ops = {
            '<': lambda a, b: a < b,
            '<=': lambda a, b: a <= b,
            '>': lambda a, b: a > b,
            '>=': lambda a, b: a >= b,
            '==': lambda a, b: abs(a - b) < 0.001,
            '!=': lambda a, b: abs(a - b) >= 0.001,
            'between': lambda a, b: b[0] <= a <= b[1],
        }

        if operator in ops:
            return ops[operator](actual_value, compare_value)
        return False

    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        if df.empty:
            return []

        signals = []
        latest = df.iloc[-1]
        price = float(latest['Close'])

        for rule in self.params['rules']:
            # All conditions must be true
            all_met = all(
                self._evaluate_condition(cond, latest)
                for cond in rule.get('conditions', [])
            )

            if all_met:
                action = rule.get('action', 'HOLD')
                signal_map = {
                    'BUY': SignalType.BUY,
                    'STRONG_BUY': SignalType.STRONG_BUY,
                    'SELL': SignalType.SELL,
                    'STRONG_SELL': SignalType.STRONG_SELL,
                    'HOLD': SignalType.HOLD,
                }

                target = price * (1 + rule.get('target_pct', 0.20)) if action in ('BUY', 'STRONG_BUY') else None
                stop = price * (1 - rule.get('stop_loss_pct', 0.08)) if action in ('BUY', 'STRONG_BUY') else None

                signals.append(StrategySignal(
                    ticker=ticker,
                    signal=signal_map.get(action, SignalType.HOLD),
                    confidence=rule.get('confidence', 0.5),
                    strategy_name=f"{self.name}:{rule['name']}",
                    price=price,
                    target_price=round(target, 2) if target else None,
                    stop_loss=round(stop, 2) if stop else None,
                    position_size_pct=rule.get('position_size', 0.05),
                    reasoning=f"Rule '{rule['name']}' triggered. All {len(rule['conditions'])} conditions met.",
                    metadata={'rule_name': rule['name']},
                ))

        return signals
