"""
Data Engine - Fetches and processes market data
"""
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.config import WATCHLIST, DATA_LOOKBACK_DAYS, RSI_PERIOD, SMA_SHORT, SMA_LONG, MACD_FAST, MACD_SLOW, MACD_SIGNAL


def fetch_stock_data(ticker, period="3y", interval="1d"):
    """Fetch historical OHLCV data for a ticker."""
    try:
        # ETH needs special handling (crypto)
        symbol = "ETH-USD" if ticker == "ETH" else ticker
        data = yf.download(symbol, period=period, interval=interval, progress=False)
        if data.empty:
            return None
        # Flatten multi-level columns if present
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        data['Ticker'] = ticker
        return data
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None


def fetch_all_watchlist(period="3y"):
    """Fetch data for all watchlist stocks."""
    all_data = {}
    for ticker in WATCHLIST:
        df = fetch_stock_data(ticker, period=period)
        if df is not None:
            all_data[ticker] = df
    return all_data


def calculate_technical_indicators(df):
    """Add technical indicators to a DataFrame."""
    df = df.copy()
    close = df['Close']

    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window=RSI_PERIOD).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    # Moving Averages
    df['SMA_20'] = close.rolling(window=SMA_SHORT).mean()
    df['SMA_50'] = close.rolling(window=SMA_LONG).mean()
    df['SMA_200'] = close.rolling(window=200).mean()
    df['EMA_12'] = close.ewm(span=MACD_FAST).mean()
    df['EMA_26'] = close.ewm(span=MACD_SLOW).mean()

    # MACD
    df['MACD'] = df['EMA_12'] - df['EMA_26']
    df['MACD_Signal'] = df['MACD'].ewm(span=MACD_SIGNAL).mean()
    df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

    # Bollinger Bands
    df['BB_Mid'] = close.rolling(window=20).mean()
    bb_std = close.rolling(window=20).std()
    df['BB_Upper'] = df['BB_Mid'] + (bb_std * 2)
    df['BB_Lower'] = df['BB_Mid'] - (bb_std * 2)

    # ATR (Average True Range)
    high, low = df['High'], df['Low']
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()

    # Volume metrics
    df['Vol_SMA_20'] = df['Volume'].rolling(window=20).mean()
    df['Vol_Ratio'] = df['Volume'] / df['Vol_SMA_20']

    # Drawdown from all-time high
    df['ATH'] = close.cummax()
    df['Drawdown_Pct'] = (close - df['ATH']) / df['ATH']

    return df


def analyze_pullback(ticker, df):
    """Analyze pullback metrics for a stock."""
    info = WATCHLIST[ticker]
    current_price = float(df['Close'].iloc[-1])
    high = info['high']
    low = info['low']

    drawdown_from_high = (current_price - high) / high
    recovery_from_low = (current_price - low) / (high - low) if high != low else 0
    upside_to_high = (high - current_price) / current_price

    latest = df.iloc[-1]
    return {
        'ticker': ticker,
        'sector': info['sector'],
        'all_time_high': high,
        'pullback_low': low,
        'current_price': round(current_price, 2),
        'drawdown_pct': round(drawdown_from_high * 100, 1),
        'recovery_pct': round(recovery_from_low * 100, 1),
        'upside_to_ath_pct': round(upside_to_high * 100, 1),
        'rsi': round(float(latest.get('RSI', 0)), 1) if pd.notna(latest.get('RSI')) else None,
        'above_sma20': bool(current_price > float(latest.get('SMA_20', 0))) if pd.notna(latest.get('SMA_20')) else None,
        'above_sma50': bool(current_price > float(latest.get('SMA_50', 0))) if pd.notna(latest.get('SMA_50')) else None,
        'macd_bullish': bool(float(latest.get('MACD_Hist', 0)) > 0) if pd.notna(latest.get('MACD_Hist')) else None,
        'vol_ratio': round(float(latest.get('Vol_Ratio', 0)), 2) if pd.notna(latest.get('Vol_Ratio')) else None,
    }


def get_full_analysis():
    """Run full analysis on all watchlist stocks."""
    print("Fetching market data...")
    all_data = fetch_all_watchlist()
    results = []
    enriched_data = {}

    for ticker, df in all_data.items():
        df = calculate_technical_indicators(df)
        enriched_data[ticker] = df
        analysis = analyze_pullback(ticker, df)
        results.append(analysis)

    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values('drawdown_pct', ascending=True)
    return results_df, enriched_data


if __name__ == "__main__":
    results, data = get_full_analysis()
    print("\n=== PULLBACK ANALYSIS ===")
    print(results.to_string(index=False))
