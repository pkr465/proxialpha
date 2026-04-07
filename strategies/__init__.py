from strategies.base import BaseStrategy, StrategySignal, SignalType
from strategies.dip_buyer import DipBuyerStrategy
from strategies.technical import TechnicalStrategy
from strategies.dca import DCAStrategy
from strategies.custom_rules import CustomRulesStrategy
from strategies.ai_strategy import AIStrategy
from strategies.momentum import MomentumStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.breakout import BreakoutStrategy
from strategies.trend_following import TrendFollowingStrategy
from strategies.pairs_trading import PairsTradingStrategy
from strategies.earnings_play import EarningsPlayStrategy
from strategies.sector_rotation import SectorRotationStrategy
from strategies.scalping import ScalpingStrategy
from strategies.swing_trading import SwingTradingStrategy
from strategies.options_flow import OptionsFlowStrategy
from strategies.strategy_manager import StrategyManager

STRATEGY_REGISTRY = {
    "DipBuyer": DipBuyerStrategy,
    "Technical": TechnicalStrategy,
    "DCA": DCAStrategy,
    "CustomRules": CustomRulesStrategy,
    "AI_Claude": AIStrategy,
    "Momentum": MomentumStrategy,
    "MeanReversion": MeanReversionStrategy,
    "Breakout": BreakoutStrategy,
    "TrendFollowing": TrendFollowingStrategy,
    "PairsTrading": PairsTradingStrategy,
    "EarningsPlay": EarningsPlayStrategy,
    "SectorRotation": SectorRotationStrategy,
    "Scalping": ScalpingStrategy,
    "SwingTrading": SwingTradingStrategy,
    "OptionsFlow": OptionsFlowStrategy,
}
