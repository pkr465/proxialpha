"""
Base Strategy - Abstract interface for all trading strategies.
All strategies implement this interface, making them hot-swappable and pluggable.
Claude AI or any external system can inject new strategies at runtime.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import pandas as pd
from datetime import datetime


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    STRONG_BUY = "STRONG_BUY"
    STRONG_SELL = "STRONG_SELL"


@dataclass
class StrategySignal:
    """Universal signal format all strategies must produce."""
    ticker: str
    signal: SignalType
    confidence: float          # 0.0 to 1.0
    strategy_name: str
    price: float
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    position_size_pct: float = 0.05  # % of portfolio
    reasoning: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            'ticker': self.ticker,
            'signal': self.signal.value,
            'confidence': self.confidence,
            'strategy': self.strategy_name,
            'price': self.price,
            'target': self.target_price,
            'stop_loss': self.stop_loss,
            'size_pct': self.position_size_pct,
            'reasoning': self.reasoning,
            'timestamp': self.timestamp.isoformat(),
            **self.metadata,
        }


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.

    To create a new strategy (including AI-powered ones):
    1. Subclass BaseStrategy
    2. Implement generate_signals()
    3. Register with StrategyManager

    The plugin architecture allows Claude or any AI to:
    - Create new strategy classes at runtime
    - Modify parameters of existing strategies
    - Override signal generation logic
    - Combine multiple strategies dynamically
    """

    def __init__(self, name: str, weight: float = 1.0, params: dict = None):
        self.name = name
        self.weight = weight  # Weight when combining with other strategies
        self.params = params or {}
        self.is_active = True

    @abstractmethod
    def generate_signals(self, ticker: str, df: pd.DataFrame, portfolio: dict = None) -> list[StrategySignal]:
        """
        Generate trading signals for a given ticker.

        Args:
            ticker: Stock symbol
            df: DataFrame with OHLCV + technical indicators
            portfolio: Current portfolio state (optional)

        Returns:
            List of StrategySignal objects
        """
        pass

    def update_params(self, new_params: dict):
        """Hot-update strategy parameters (e.g., from Claude AI)."""
        self.params.update(new_params)

    def activate(self):
        self.is_active = True

    def deactivate(self):
        self.is_active = False

    def __repr__(self):
        return f"{self.name}(weight={self.weight}, active={self.is_active})"
