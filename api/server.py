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
  GET  /api/diary                  Read trading diary (JSONL)
  GET  /api/llm-logs                Tail raw LLM request/response log
  GET  /api/risk/summary           Current RiskManager configuration
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
from fastapi import Request as _Request
from fastapi import Response as _Response
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

# ---- OBSERVABILITY (P2-4: JSON logs, /metrics, request middleware) ----
# Installed FIRST so the structured log formatter is in place before any
# other module emits its first log line, and the request middleware sees
# every downstream middleware's outcome (auth, CORS, rate limit). The
# install function is idempotent and falls open if optional deps are
# missing — see api/observability.py for the rationale on not making
# structlog/opentelemetry/prometheus_client hard dependencies.
try:
    from api.observability import install_observability
    install_observability(app)
except Exception as _obs_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "observability layer not installed: %s", _obs_exc
    )

# ---- CORS (P1-1: locked to allow-list) ----
# We deliberately do NOT use ``["*"]`` here. The dashboard sends
# credentialed requests (Clerk session cookies + tenant headers), and
# wildcard origins + credentials = a self-serve cross-tenant data leak.
# The allow-list is read from ``settings.cors_allowed_origins`` (CSV),
# which defaults to the local dev dashboard. Production env MUST set
# ``CORS_ALLOWED_ORIGINS`` explicitly. An empty string disables CORS
# entirely (useful for backend-only deployments).
try:
    from core.settings import get_settings as _get_settings_for_cors

    _cors_csv = _get_settings_for_cors().cors_allowed_origins or ""
    _cors_origins = [o.strip() for o in _cors_csv.split(",") if o.strip()]
except Exception:  # pragma: no cover - settings unavailable in trading-only checkout
    _cors_origins = ["http://localhost:3000"]

if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Stub-User-Email",
            "X-Stub-Org-Id",
            "Stripe-Signature",
        ],
    )

# ---- AUTH (P0-2: Clerk JWT verifier with stub fallback) ----
# :class:`ClerkAuthMiddleware` is the production auth path. It verifies
# a ``Authorization: Bearer <jwt>`` against Clerk's JWKS, JIT-creates
# ``users`` / ``organizations`` rows on first sight, and writes
# ``user`` + ``org_id`` to ``request.state``. When ``CLERK_ISSUER`` is
# unset (local dev / test) it falls back to reading the stub headers
# the old :class:`AuthStubMiddleware` used, so the existing test suite
# keeps working unchanged.
#
# Wrapped in try/except so a missing pyjwt/cryptography in a
# trading-only dev checkout doesn't crash the server at import time.
try:
    from api.middleware.clerk_auth import ClerkAuthMiddleware
    app.add_middleware(ClerkAuthMiddleware)
except Exception as _auth_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "clerk auth middleware not installed: %s", _auth_exc
    )
    # Fall back to the bare stub so endpoints that read
    # ``request.state.user`` still find a populated value in dev.
    try:
        from api.middleware.auth_stub import AuthStubMiddleware
        app.add_middleware(AuthStubMiddleware)
    except Exception as _auth_stub_exc:  # pragma: no cover
        _logging.getLogger(__name__).warning(
            "auth stub middleware also not installed: %s", _auth_stub_exc
        )

# ---- BILLING (Task 02: Stripe webhook handler) ----
# Imported here rather than at the top of the file so a missing pydantic-
# settings / stripe / asyncpg dependency in a local trading-only checkout
# can't take down the existing endpoints. In production all deps are
# installed from ``requirements.txt`` and this always succeeds.
try:
    from api.billing import billing_router, read_router
    app.include_router(billing_router, prefix="/api/billing", tags=["billing"])
    # The entitlements read endpoint lives at the top-level ``/api``
    # path per spec §7.4, not under ``/api/billing``. Mount it
    # separately so the URL shape matches the spec exactly.
    app.include_router(read_router, prefix="/api", tags=["billing"])
except Exception as _billing_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "billing router not registered: %s", _billing_exc
    )

# ---- INSTALL TOKEN ADMIN (P1-4: dashboard issuer for one-shot tokens) ----
# Mounted under ``/api`` to match the existing dashboard URL shape
# (the route itself is ``/orgs/{org_id}/install-tokens``, so the full
# path becomes ``/api/orgs/{org_id}/install-tokens``). This file does
# NOT pull in Stripe — see the install_tokens_admin module docstring
# for the rationale on splitting it out from api.billing.endpoints.
try:
    from api.billing.install_tokens_admin import router as install_tokens_router
    app.include_router(install_tokens_router, prefix="/api", tags=["admin"])
except Exception as _install_tokens_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "install-tokens admin router not registered: %s", _install_tokens_exc
    )

# ---- SUPPORT BUNDLE UPLOAD (P2-7: doctor bundle ingest) ----
# Receives ``.tar.gz`` bundles produced by ``proxialpha doctor`` so
# operators no longer have to email them. The full route is
# ``/api/support/bundles`` and accepts BOTH Clerk-authed dashboard
# users (e.g. an admin re-uploading a customer bundle on their
# behalf) and unenrolled agents holding a fresh install-token. The
# install-token path uses the read-only ``lookup_install_token``
# variant so a doctor run does not consume the token the agent still
# needs for its own enrollment. Wrapped in the same optional-dep
# fallback as the other billing routers.
try:
    from api.billing.support_bundles import router as support_bundles_router
    app.include_router(support_bundles_router, prefix="/api", tags=["support"])
except Exception as _support_bundles_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "support-bundles router not registered: %s", _support_bundles_exc
    )

# ---- /.well-known/jwks.json (P0-3: JWKS rotation) ----
# RFC 8615 well-known endpoint that publishes the control plane's
# public signing keys (current + previous, both with stable ``kid``
# values) so enrolled agents can refresh their key cache mid-rotation
# without redeploying. The route is mounted at the absolute path
# ``/.well-known/jwks.json`` with NO prefix — RFC 8615 requires the
# well-known segment to live at the root.
try:
    from api.wellknown import router as wellknown_router
    app.include_router(wellknown_router, tags=["wellknown"])
except Exception as _wellknown_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "wellknown JWKS router not registered: %s", _wellknown_exc
    )

# ---- AGENT HEARTBEAT (Task 06: Phase 2 — Customer Agent) ----
# Wrapped in the same try/except pattern as the billing router so a
# trading-only dev checkout without PyJWT / cryptography doesn't
# crash the server at import time. In production both deps are
# installed from ``requirements.txt`` and this always succeeds.
#
# The route is mounted at ``/agent/heartbeat`` (NOT under ``/api/``)
# per ADR-003 — agent traffic has its own URL prefix so reverse
# proxies can route / rate-limit / auth it separately from the
# dashboard API.
try:
    from api.agent import agent_router
    app.include_router(agent_router, prefix="/agent", tags=["agent"])
except Exception as _agent_exc:  # pragma: no cover - optional dep fallback
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "agent router not registered: %s", _agent_exc
    )

# ---- ENTITLEMENTS FEATURE FLAG (P1-2: default ON) ----
# Wraps paid routes with the ``requires_entitlement`` decorator. The
# default is now ON (safe-by-default) — set ``ENTITLEMENTS_ENABLED=0``
# in local dev if you want to bypass the gate. Reading from
# ``settings.entitlements_enabled`` keeps the env-var contract
# (``ENTITLEMENTS_ENABLED``) for backwards compat via the pydantic
# field's alias.
import os as _os
try:
    from core.settings import get_settings as _get_settings_for_ent

    ENTITLEMENTS_ENABLED = _get_settings_for_ent().entitlements_enabled
except Exception:  # pragma: no cover - settings unavailable
    # Fall back to the old env-var behaviour but with the new safe
    # default (ON) so a missing pydantic-settings install can't
    # accidentally turn the gate off in production.
    ENTITLEMENTS_ENABLED = _os.environ.get("ENTITLEMENTS_ENABLED", "1") == "1"


# ---- ROUTE-LEVEL GATING HELPERS (P1-3: legacy trading audit) ----
#
# The gap analysis (docs/specs/phase2-go-live-gap-analysis.md §P1-3)
# called out that ``/api/llm/analyze`` was the ONLY route wrapped by
# the entitlements decorator. Every other route in this file has been
# audited; the audit table at the bottom of this section records the
# disposition of each one.
#
# The decorator is wrapped in a small ``_gated`` factory so we don't
# repeat the "if ENTITLEMENTS_ENABLED + try/except optional dep"
# dance at every call site. ``_gated`` is a no-op when the gate is
# off OR when ``core.entitlements`` is missing (e.g. trading-only
# dev checkout) — in both cases the decorator returns the function
# unchanged so the route still works for tests.
def _gated(feature: str, consume: int = 1):
    if not ENTITLEMENTS_ENABLED:
        return lambda f: f
    try:
        from core.entitlements import requires_entitlement
    except Exception as _gate_exc:  # pragma: no cover - optional dep fallback
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "_gated(%s): entitlements decorator not installed: %s",
            feature,
            _gate_exc,
        )
        return lambda f: f
    return requires_entitlement(feature, consume=consume)


# Helper to open a short-lived AsyncSession for in-route flag/cap
# checks (the boolean and cap gates do not need the consume path's
# atomic UPDATE — see api/billing/feature_gates.py for the rationale).
# Returns ``None`` if asyncpg / pydantic-settings are not installed in
# the current checkout, in which case the calling route should treat
# the check as "skipped" and continue.
def _open_billing_session():  # -> AsyncContextManager[AsyncSession] | None
    if not ENTITLEMENTS_ENABLED:
        return None
    try:
        from core.entitlements import _session_factory  # uses test-overridable factory
    except Exception:  # pragma: no cover - optional dep fallback
        return None
    return _session_factory()


# AUDIT TABLE — disposition of every route in this file as of P1-3.
# Updates here are LOAD-BEARING: when a new route is added, this
# block must be updated in the same PR or the gap re-opens.
#
#   Route                          Method  Disposition         Gate
#   /api/health                    GET     unauthenticated     none
#   /api/strategies                GET     read-only           none
#   /api/strategies/activate       POST    boolean (custom)    custom_strategies
#   /api/strategies/weights        POST    in-process tweak    none (covered by tier on /activate)
#   /api/watchlist                 GET     read-only           none
#   /api/watchlist/add             POST    cap (tickers)       inline assert_within_cap
#   /api/scan                      GET     in-process pandas   none (no LLM, no DB)
#   /api/portfolio                 GET     read-only           none
#   /api/trade                     POST    boolean (live)      conditional assert_feature_flag
#   /api/backtest                  GET     consumable          @_gated("backtests", 1)
#   /api/performance               GET     read-only           none
#   /api/llm/providers             GET     read-only           none
#   /api/llm/configure             POST    config write        none (provider/key in body)
#   /api/llm/analyze               POST    consumable          @_gated("signals", 1)
#   /api/diary                     GET     read-only           none
#   /api/llm-logs                  GET     read-only           none
#   /api/risk/summary              GET     read-only           none
#   /ws/signals, /ws/prices        WS      read-only           none
#
# Routes flagged "in-process pandas" (e.g. /api/scan) deliberately
# stay un-gated: they don't talk to a paid provider and they don't
# decrement a quota. If we later push them onto a metered backend
# they'll move to a consumable feature.
#
# Routes mounted by other routers (api.billing, api.agent,
# api.wellknown, api.billing.install_tokens_admin) carry their own
# auth/entitlement decisions inside those modules and are out of
# scope for this audit.


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
async def toggle_strategy(req: StrategyToggle, request: _Request):
    if req.name not in STRATEGY_REGISTRY:
        raise HTTPException(404, f"Strategy '{req.name}' not found")

    # P1-3: registering a NEW strategy (one not already in the
    # manager's roster) counts as enabling a custom strategy slot.
    # Re-toggling an already-registered strategy is free for any tier
    # — the work-of-art is the registration itself, not the on/off bit.
    # Free-tier orgs that haven't paid for ``custom_strategies`` get
    # blocked at registration time.
    if req.name not in state.manager.strategies and req.active:
        org_id = getattr(request.state, "org_id", None)
        if ENTITLEMENTS_ENABLED and org_id is not None:
            try:
                from api.billing.feature_gates import assert_feature_flag
            except Exception as _gate_exc:  # pragma: no cover - optional dep fallback
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "strategies/activate: custom_strategies gate skipped: %s",
                    _gate_exc,
                )
            else:
                session_cm = _open_billing_session()
                if session_cm is not None:
                    async with session_cm as _session:
                        await assert_feature_flag(
                            _session, org_id, "custom_strategies"
                        )

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
async def add_to_watchlist(req: WatchlistAdd, request: _Request):
    # P1-3: enforce the per-tier ``tickers`` cap before mutating
    # WATCHLIST. Adding ticker T raises the proposed total to
    # ``len(current_watchlist) + 1`` (or stays put if T is already
    # present, in which case the cap is trivially satisfied).
    org_id = getattr(request.state, "org_id", None)
    if ENTITLEMENTS_ENABLED and org_id is not None and req.ticker not in WATCHLIST:
        try:
            from api.billing.feature_gates import assert_within_cap
        except Exception as _gate_exc:  # pragma: no cover - optional dep fallback
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "watchlist/add: tickers gate skipped: %s", _gate_exc
            )
        else:
            session_cm = _open_billing_session()
            if session_cm is not None:
                async with session_cm as _session:
                    await assert_within_cap(
                        _session,
                        org_id,
                        "tickers",
                        proposed_count=len(WATCHLIST) + 1,
                    )

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
async def execute_trade(req: TradeRequest, request: _Request):
    if not state.trader:
        raise HTTPException(500, "Paper trading not initialized")

    if req.ticker not in state.data:
        raise HTTPException(404, f"Ticker {req.ticker} not in watchlist")

    # P1-3: gate live trading behind the per-tier ``live_trading`` flag.
    # The current build wires ``state.trader`` to PaperTrader, so this
    # check is a defence-in-depth no-op today. The moment a real broker
    # adapter (live_trading/alpaca_bot.py, hyperliquid_bot.py) is
    # plumbed into ``state.trader`` and exposes ``is_live = True``, the
    # gate kicks in WITHOUT another audit pass. We deliberately make
    # the predicate "is this trader live?" rather than relying on a
    # separate route, because the gap analysis specifically called out
    # that a paper-only customer must not be able to drive a live
    # broker via this same endpoint.
    is_live = bool(getattr(state.trader, "is_live", False))
    org_id = getattr(request.state, "org_id", None)
    if is_live and ENTITLEMENTS_ENABLED and org_id is not None:
        try:
            from api.billing.feature_gates import assert_feature_flag
        except Exception as _gate_exc:  # pragma: no cover - optional dep fallback
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "trade: live_trading gate skipped: %s", _gate_exc
            )
        else:
            session_cm = _open_billing_session()
            if session_cm is not None:
                async with session_cm as _session:
                    await assert_feature_flag(_session, org_id, "live_trading")

    price = float(state.data[req.ticker]['Close'].iloc[-1])

    if req.action.upper() == "BUY":
        result = state.trader.execute_buy(req.ticker, price, req.shares, req.dollar_amount)
    elif req.action.upper() == "SELL":
        result = state.trader.execute_sell(req.ticker, price, req.shares)
    else:
        raise HTTPException(400, f"Invalid action: {req.action}")

    await broadcast({"type": "trade_executed", "trade": result})
    return {"trade": result}


# P1-3: backtests are a metered consumable. The route always declares
# ``request`` and ``response`` so flipping ENTITLEMENTS_ENABLED on/off
# does not change the signature — FastAPI injects them either way.
async def _run_backtest_impl(
    capital: float,
    days: int,
    request: _Request,
    response: _Response,
):
    engine = BacktestEngine(state.manager, capital)
    results = engine.run(state.data)
    summary = engine.get_summary()
    curve = results['equity_curve'].to_dict('records') if not results['equity_curve'].empty else []
    return {"summary": summary, "equity_curve": curve[-100:]}  # Last 100 points


@app.get("/api/backtest")
@_gated("backtests", consume=1)
async def run_backtest(
    request: _Request,
    response: _Response,
    capital: float = Query(DEFAULT_CAPITAL),
    days: int = Query(504),
):
    return await _run_backtest_impl(capital, days, request, response)


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


# ---- DIARY / LLM LOG ROUTES ----
@app.get("/api/diary")
async def get_diary_entries(
    limit: int = Query(200, ge=1, le=2000),
    event: Optional[str] = Query(None),
    source: str = Query("paper", pattern="^(paper|live|backtest|ai)$"),
):
    """Return recent diary entries, newest first.

    Args:
        limit: number of entries to return
        event: optional event_type filter (trade_executed, trade_rejected, decision, ...)
        source: which diary file to read (paper/live/backtest/ai)
    """
    try:
        from core.diary import get_diary
    except ImportError:
        return {"entries": [], "error": "diary module unavailable"}
    path_map = {
        "paper": "data/paper_diary.jsonl",
        "live": "data/live_diary.jsonl",
        "backtest": "data/backtest_diary.jsonl",
        "ai": "data/diary.jsonl",
    }
    diary = get_diary(path_map[source])
    return {"entries": diary.read(limit=limit, event_filter=event), "source": source}


@app.get("/api/llm-logs")
async def get_llm_logs(n_bytes: int = Query(20000, ge=1000, le=500000)):
    """Return the last ``n_bytes`` of the raw LLM request/response log."""
    try:
        from core.diary import get_llm_log
    except ImportError:
        return {"log": "", "error": "diary module unavailable"}
    llm_log = get_llm_log()
    return {"log": llm_log.tail(n_bytes=n_bytes), "path": str(llm_log.path)}


@app.get("/api/risk/summary")
async def get_risk_summary():
    """Return the active RiskManager configuration."""
    try:
        from core.risk_manager import RiskManager
    except ImportError:
        return {"error": "risk_manager unavailable"}
    rm = getattr(state.trader, "risk_manager", None) or RiskManager()
    return {"risk": rm.get_risk_summary()}


# Sentinel application of the entitlement decorator. Task 04 gates the
# LLM-backed "signal" generation route behind the ``signals`` feature
# quota. The decorator is applied conditionally so the existing
# integration tests (which have no billing DB) keep working: when
# ``ENTITLEMENTS_ENABLED`` is off the route runs unchanged; when on, the
# decorator reads ``request.state.org_id`` (set by AuthStubMiddleware)
# and either consumes 1 quota unit or returns 402.
#
# The route declares ``request`` and ``response`` regardless of the flag
# so enabling ``ENTITLEMENTS_ENABLED`` later does not require a signature
# change — FastAPI injects both either way.
async def _llm_analyze_impl(
    req: LLMAnalyzeRequest,
    request: _Request,
    response: _Response,
):
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

    llm_response = state.llm.analyze(json.dumps(context, default=str), req.prompt)
    return {"response": llm_response.text, "model": llm_response.model, "provider": llm_response.provider}


# P1-3: simplified to use the shared ``_gated`` factory. The factory
# already handles ``ENTITLEMENTS_ENABLED`` off + missing optional dep,
# so the three branches we used to maintain (gated / fallback / no-op)
# collapse to one.
@app.post("/api/llm/analyze")
@_gated("signals", consume=1)
async def llm_analyze(
    req: LLMAnalyzeRequest,
    request: _Request,
    response: _Response,
):
    return await _llm_analyze_impl(req, request, response)


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
