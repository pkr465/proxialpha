"""
Demo Data Generator - Creates realistic sample data for testing
when yfinance API is unavailable (sandbox/offline mode).
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.config import WATCHLIST


def generate_demo_stock(ticker, days=504):
    """Generate realistic OHLCV data for a stock based on its pullback profile."""
    info = WATCHLIST[ticker]
    high = info['high']
    low = info['low']
    np.random.seed(hash(ticker) % 2**31)

    dates = pd.date_range(end=datetime.now(), periods=days, freq='B')

    # Simulate a rise to ATH then pullback
    peak_day = int(days * 0.35)
    prices = np.zeros(days)

    # Phase 1: Rise to ATH
    start = low + (high - low) * 0.4
    for i in range(peak_day):
        progress = i / peak_day
        target = start + (high - start) * progress
        noise = np.random.normal(0, (high - low) * 0.01)
        prices[i] = target + noise

    # Phase 2: Pullback from ATH
    current = low + (high - low) * np.random.uniform(0.05, 0.25)
    for i in range(peak_day, days):
        progress = (i - peak_day) / (days - peak_day)
        target = high - (high - current) * progress
        noise = np.random.normal(0, (high - low) * 0.008)
        bounce = np.sin(progress * np.pi * 4) * (high - low) * 0.03
        prices[i] = max(target + noise + bounce, low * 0.9)

    prices = np.clip(prices, low * 0.8, high * 1.05)

    # Generate OHLCV
    df = pd.DataFrame(index=dates)
    df['Close'] = prices
    df['Open'] = prices * (1 + np.random.normal(0, 0.005, days))
    df['High'] = np.maximum(df['Open'], df['Close']) * (1 + np.abs(np.random.normal(0, 0.01, days)))
    df['Low'] = np.minimum(df['Open'], df['Close']) * (1 - np.abs(np.random.normal(0, 0.01, days)))
    df['Volume'] = np.random.lognormal(15, 0.5, days).astype(int)
    df['Ticker'] = ticker

    return df


def generate_all_demo_data():
    """Generate demo data for all watchlist stocks."""
    return {ticker: generate_demo_stock(ticker) for ticker in WATCHLIST}
