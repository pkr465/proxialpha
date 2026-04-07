"""
ProxiAlpha - Configuration
Pullback stocks watchlist and global settings
"""

# Watchlist from pullback analysis
WATCHLIST = {
    "COIN":  {"high": 443, "low": 138, "sector": "Crypto/Fintech"},
    "HOOD":  {"high": 153, "low": 63,  "sector": "Crypto/Fintech"},
    "ORCL":  {"high": 345, "low": 136, "sector": "Tech"},
    "MSTR":  {"high": 543, "low": 105, "sector": "Crypto/Tech"},
    "ETH":   {"high": 4956,"low": 1795,"sector": "Crypto"},
    "NOW":   {"high": 239, "low": 98,  "sector": "Tech/SaaS"},
    "SOFI":  {"high": 32,  "low": 15,  "sector": "Fintech"},
    "HIMS":  {"high": 72,  "low": 13,  "sector": "Healthcare"},
    "NKE":   {"high": 179, "low": 44,  "sector": "Consumer/Retail"},
    "NVO":   {"high": 148, "low": 36,  "sector": "Healthcare"},
    "UNH":   {"high": 632, "low": 234, "sector": "Healthcare"},
    "IREN":  {"high": 76,  "low": 30,  "sector": "Crypto/Mining"},
    "TGT":   {"high": 269, "low": 83,  "sector": "Consumer/Retail"},
    "EL":    {"high": 373, "low": 69,  "sector": "Consumer"},
    "LULU":  {"high": 516, "low": 143, "sector": "Consumer/Retail"},
}

# Trading parameters
DEFAULT_CAPITAL = 100000
MAX_POSITION_SIZE = 0.10       # 10% max per position
STOP_LOSS_PCT = 0.08           # 8% stop loss
TAKE_PROFIT_PCT = 0.25         # 25% take profit
DCA_INTERVALS = 5              # Number of DCA tranches
COMMISSION_PER_TRADE = 0.00    # Commission (Alpaca = $0)

# Technical indicator settings
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
SMA_SHORT = 20
SMA_LONG = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Pullback strategy levels
DIP_BUY_LEVELS = [0.30, 0.50, 0.60, 0.70]  # Buy at 30%, 50%, 60%, 70% drawdown from high
RECOVERY_TARGETS = [0.25, 0.50, 0.75, 1.00]  # Sell at 25%, 50%, 75%, 100% recovery

# Alpaca API (user fills these in)
ALPACA_API_KEY = "YOUR_API_KEY"
ALPACA_SECRET_KEY = "YOUR_SECRET_KEY"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading by default

# Data settings
DATA_LOOKBACK_DAYS = 365 * 3  # 3 years of historical data
