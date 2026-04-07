"""
FastAPI Backend Server - REST API + WebSocket for real-time updates.
Serves both the React web dashboard and React Native mobile app.

Run: uvicorn api.server:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
  GET  /api/health                 Health check
  GET  /api/strategies             List all available strategies
  POST /api/strategies/activate    Activate/deactivate strategies
  POST /api/strategies/weights     Update strategy weights
  GET  /api/watchlist              Get watchlist with current analysis
  POST /api/watchlist/add          Add ticker to watchlist
  POST /api/watchlist/remove       Remove ticker from watchlist
  GET  /api/scan                   Run full scan and get signals
  GET  /api/portfolio              Get portfolio state
  POST /api/trade                  Execute paper/live trade
  GET  /api/backtest               Run backtest
  GET  /api/performance            Get performance metrics
  GET  /api/llm/providers          List available LLM providers
  POST /api/llm/analyze            Send analysis request to LLM
  POST /api/llm/configure          Configure LLM provider
  WS   /ws/signals                 Real-time signal stream
  WS   /ws/prices                  Real-time price updates
"""
import asyncio
import json
import sys
import os
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import WATCHLIST, DEFAULT_CAPITAL
from core.data_engine import fetch_stock_data, calculate_technical_indicators, analyze_pullback
from core.demo_data import generate_all_demo_data
from core.llm_adapter import LLMAdapter, LLMConfig
from strategies import STRATEGY_REGISTRY
from strategies.strategy_manager import StrategyManager
from strategies.base import SignalType
from paper_trading.simulator import PaperTrader
from backtesting.engine import BacktestEngine


# ---- STATE ----
class AppState:
    def __init__(self):
        self.manager = StrategyManager()
        self.data = {}
        self.active_strategies = {}
        self.trader = None
        self.llm = None
        self.ws_clients: list[WebSocket] = []
        self.use_demo = True  # Toggle for demo/live data

    def init_default_strategies(self):
        defaults = {
            "DipBuyer": {"weight": 1.2, "active": True},
            "Technical": {"weight": 1.0, "active": True},
            "Momentum": {"weight": 1.0, "active": True},
            "MeanReversion": {"weight": 0.9, "active": True},
            "Breakout": {"weight": 1.0, "active": False},
            "TrendFollowing": {"weight": 1.1, "active": False},
            "DCA": {"weight": 0.8, "active": False},
            "PairsTrading": {"weight": 0.7, "active": False},
            "EarningsPlay": {"weight": 0.8, "active": False},
            "SectorRotation": {"weight": 0.9, "active": False},
            "Scalping": {"weight": 0.6, "active": False},
            "SwingTrading": {"weight": 1.0, "active": False},
            "OptionsFlow": {"weight": 0.8, "active": False},
            "CustomRules": {"weight": 0.9, "active": False},
        }
        for name, cfg in defaults.items():
            cls = STRATEGY_REGISTRY.get(name)
            if cls:
                strat = cls(weight=cfg["weight"])
                if not cfg["active"]:
                    strat.deactivate()
                self.manager.register_strategy(strat)
                self.active_strategies[name] = cfg

    def load_data(self):
        if self.use_demo:
            raw = generate_all_demo_data()
        else:
            from core.data_engine import fetch_all_watchlist
            raw = fetch_all_watchlist()

        self.data = {}
        for ticker, df in raw.items():
            self.data[ticker] = calculate_technical_indicators(df)


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.init_default_strategies()
    state.load_data()
    state.trader = PaperTrader(
        state.manager,
        state_file=os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_trading_state.json"),
    )
    yield


app = FastAPI(
    title="ProxiAlpha API",
    description="ProxiAlpha - AI-powered multi-strategy trading platform | proxiant.ai/proxialpha",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- MODELS ----
class StrategyToggle(BaseModel):
    name: str
    active: bool

class StrategyWeights(BaseModel):
    weights: dict[str, float]

class WatchlistAdd(BaseModel):
    ticker: str
    high: float
    low: float
    sector: str = "Unknown"

class TradeRequest(BaseModel):
    ticker: str
    action: str  # BUY or SELL
    shares: Optional[int] = None
    dollar_amount: Optional[float] = None

class LLMConfigRequest(BaseModel):
    provider: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None

class LLMAnalyzeRequest(BaseModel):
    ticker: Optional[str] = None
    prompt: str = "Analyze the current market conditions"
    include_portfolio: bool = False


# ---- ROUTES ----
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat(),
            "strategies_loaded": len(state.manager.strategies),
            "tickers_loaded": len(state.data)}


@app.get("/api/strategies")
async def list_strategies():
    result = []
    for name, cls in STRATEGY_REGISTRY.items():
        strat = state.manager.strategies.get(name)
        result.append({
            "name": name,
            "active": strat.is_active if strat else False,
            "weight": strat.weight if strat else 1.0,
            "registered": name in state.manager.strategies,
            "category": _strategy_category(name),
            "description": _strategy_description(name),
        })
    return {"strategies": result}


@app.post("/api/strategies/activate")
async def toggle_strategy(req: StrategyToggle):
    if req.name not in STRATEGY_REGISTRY:
        raise HTTPException(404, f"Strategy '{req.name}' not found")

    if req.name not in state.manager.strategies:
        cls = STRATEGY_REGISTRY[req.name]
        strat = cls(weight=1.0)
        state.manager.register_strategy(strat)

    strat = state.manager.strategies[req.name]
    if req.active:
        strat.activate()
    else:
        strat.deactivate()

    state.active_strategies[req.name] = {"weight": strat.weight, "active": req.active}
    await broadcast({"type": "strategy_update", "name": req.name, "active": req.active})
    return {"name": req.name, "active": req.active}


@app.post("/api/strategies/weights")
async def update_weights(req: StrategyWeights):
    state.manager.set_weights(req.weights)
    await broadcast({"type": "weights_update", "weights": req.weights})
    return {"updated": req.weights}


@app.get("/api/watchlist")
async def get_watchlist():
    results = []
    for ticker in WATCHLIST:
        if ticker in state.data:
            analysis = analyze_pullback(ticker, state.data[ticker])
            results.append(analysis)
    return {"watchlist": sorted(results, key=lambda x: x['drawdown_pct'])}


@app.post("/api/watchlist/add")
async def add_to_watchlist(req: WatchlistAdd):
    WATCHLIST[req.ticker] = {"high": req.high, "low": req.low, "sector": req.sector}
    # Fetch data for new ticker
    df = fetch_stock_data(req.ticker, period="6mo")
    if df is not None:
        state.data[req.ticker] = calculate_technical_indicators(df)
    return {"added": req.ticker}


@app.get("/api/scan")
async def run_scan():
    import math
    if not state.data:
        state.load_data()
    results = state.manager.scan_all_tickers(state.data)
    scan_data = results.to_dict('records')
    # Sanitize NaN/inf values (not JSON compliant)
    for row in scan_data:
        for k, v in row.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                row[k] = None
    await broadcast({"type": "scan_complete", "signals": scan_data, "timestamp": datetime.now().isoformat()})
    return {"signals": scan_data, "timestamp": datetime.now().isoformat()}


@app.get("/api/portfolio")
async def get_portfolio():
    if not state.trader:
        return {"error": "Paper trading not initialized"}
    perf = state.trader.get_performance()
    return {"portfolio": perf, "state": state.trader.state}


@app.post("/api/trade")
async def execute_trade(req: TradeRequest):
    if not state.trader:
        raise HTTPException(500, "Paper trading not initialized")

    if req.ticker not in state.data:
        raise HTTPException(404, f"Ticker {req.ticker} not in watchlist")

    price = float(state.data[req.ticker]['Close'].iloc[-1])

    if req.action.upper() == "BUY":
        result = state.trader.execute_buy(req.ticker, price, req.shares, req.dollar_amount)
    elif req.action.upper() == "SELL":
        result = state.trader.execute_sell(req.ticker, price, req.shares)
    else:
        raise HTTPException(400, f"Invalid action: {req.action}")

    await broadcast({"type": "trade_executed", "trade": result})
    return {"trade": result}


@app.get("/api/backtest")
async def run_backtest(capital: float = Query(DEFAULT_CAPITAL), days: int = Query(504)):
    engine = BacktestEngine(state.manager, capital)
    results = engine.run(state.data)
    summary = engine.get_summary()
    curve = results['equity_curve'].to_dict('records') if not results['equity_curve'].empty else []
    return {"summary": summary, "equity_curve": curve[-100:]}  # Last 100 points


@app.get("/api/performance")
async def get_performance():
    if not state.trader:
        return {"performance": {}}
    return {"performance": state.trader.get_performance()}


# ---- LLM ROUTES ----
@app.get("/api/llm/providers")
async def llm_providers():
    return {"providers": LLMAdapter.available_providers()}


@app.post("/api/llm/configure")
async def configure_llm(req: LLMConfigRequest):
    state.llm = LLMAdapter(LLMConfig(
        provider=req.provider, model=req.model,
        api_key=req.api_key, base_url=req.base_url,
    ))
    return {"configured": req.provider, "model": req.model}


@app.post("/api/llm/analyze")
async def llm_analyze(req: LLMAnalyzeRequest):
    if not state.llm:
        raise HTTPException(400, "LLM not configured. Call POST /api/llm/configure first.")

    context = {}
    if req.ticker and req.ticker in state.data:
        df = state.data[req.ticker]
        latest = df.iloc[-1]
        context = {
            'ticker': req.ticker,
            'price': float(latest['Close']),
            'rsi': float(latest.get('RSI', 0)) if pd.notna(latest.get('RSI')) else None,
            'macd_hist': float(latest.get('MACD_Hist', 0)) if pd.notna(latest.get('MACD_Hist')) else None,
        }

    if req.include_portfolio and state.trader:
        context['portfolio'] = state.trader.get_performance()

    response = state.llm.analyze(json.dumps(context, default=str), req.prompt)
    return {"response": response.text, "model": response.model, "provider": response.provider}


# ---- WEBSOCKET ----
async def broadcast(data: dict):
    """Broadcast message to all connected WebSocket clients."""
    for ws in state.ws_clients[:]:
        try:
            await ws.send_json(data)
        except Exception:
            state.ws_clients.remove(ws)


@app.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket):
    await websocket.accept()
    state.ws_clients.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "scan":
                results = state.manager.scan_all_tickers(state.data)
                await websocket.send_json({
                    "type": "scan_result",
                    "signals": results.to_dict('records'),
                    "timestamp": datetime.now().isoformat(),
                })
    except WebSocketDisconnect:
        state.ws_clients.remove(websocket)


@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    await websocket.accept()
    state.ws_clients.append(websocket)
    try:
        while True:
            prices = {}
            for ticker, df in state.data.items():
                prices[ticker] = float(df['Close'].iloc[-1])
            await websocket.send_json({"type": "prices", "data": prices, "timestamp": datetime.now().isoformat()})
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        state.ws_clients.remove(websocket)


# ---- HELPERS ----
def _strategy_category(name):
    categories = {
        "DipBuyer": "Value", "MeanReversion": "Value",
        "Momentum": "Trend", "TrendFollowing": "Trend", "Breakout": "Trend",
        "DCA": "Accumulation", "SectorRotation": "Macro",
        "Technical": "Technical", "Scalping": "Short-Term", "SwingTrading": "Short-Term",
        "PairsTrading": "Arbitrage", "OptionsFlow": "Flow", "EarningsPlay": "Event",
        "CustomRules": "Custom", "AI_Claude": "AI",
    }
    return categories.get(name, "Other")


def _strategy_description(name):
    descriptions = {
        "DipBuyer": "Buy stocks at fixed pullback levels from all-time highs",
        "Technical": "RSI, MACD, Moving Averages scoring system",
        "DCA": "Dollar-cost averaging into pulled-back stocks",
        "CustomRules": "User-definable JSON rule engine",
        "AI_Claude": "LLM-powered signal generation via Claude/OpenAI/Ollama",
        "Momentum": "Rate of change, acceleration, and relative strength",
        "MeanReversion": "Z-score and Bollinger Band mean reversion",
        "Breakout": "Price breakout above consolidation with volume confirmation",
        "TrendFollowing": "Multi-timeframe moving averages + ADX trend detection",
        "PairsTrading": "Statistical arbitrage between correlated stocks",
        "EarningsPlay": "Pre-earnings momentum and post-earnings drift",
        "SectorRotation": "Rotate capital into strongest sectors",
        "Scalping": "Short-term reversals using volume spikes and candle patterns",
        "SwingTrading": "Multi-day trades at support/resistance and Fibonacci levels",
        "OptionsFlow": "Unusual volume and institutional accumulation detection",
    }
    return descriptions.get(name, "")


import pandas as pd  # needed for pd.notna in llm_analyze


# ---- FRONTEND ----
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    """Serve the ProxiAlpha web dashboard at the root URL."""
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>ProxiAlpha</h1><p>Dashboard not found. Place index.html in web/</p>", status_code=404)


@app.get("/proxialpha", response_class=HTMLResponse)
async def serve_frontend_alt():
    """Alternate route for proxiant.ai/proxialpha."""
    return await serve_frontend()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
