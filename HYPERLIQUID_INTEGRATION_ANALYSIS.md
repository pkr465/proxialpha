# Hyperliquid Trading Agent → ProxiAlpha Integration Analysis

**Date:** April 10, 2026
**Scope:** Analyze `hyperliquid-trading-agent` and propose an integration plan into `proxialpha`.

---

## 1. What the Hyperliquid Trading Agent Does

The `hyperliquid-trading-agent` is a **Claude-powered autonomous perpetual-futures trading bot** built specifically for the Hyperliquid DEX. It's compact (~1,900 lines of core Python), async-first, and production-grade.

### Core loop
On every interval (default 5m / 1h):

1. Fetches account state (balance, positions, PnL) from Hyperliquid.
2. **Force-closes** any position underwater by ≥ 20 % *before* any new decision is made.
3. Gathers a rich market context: OHLCV candles, funding rates, mark/liquidation prices, open orders, recent fills, an on-disk "diary" of prior actions.
4. Sends that context to **Claude Sonnet 4** with a strict system prompt framing it as a "quantitative trader / mathematician-engineer."
5. Claude may call a **`fetch_indicator` tool** mid-inference to pull fresh EMA / RSI / MACD / ATR / Bollinger / ADX / OBV / VWAP / Stoch-RSI on-demand for any asset × timeframe.
6. Claude returns a strict JSON with `trade_decisions[]` (action, size, order type, TP, SL, `exit_plan`, rationale). A cheap Haiku model is used as a **fallback sanitizer** if the JSON is malformed.
7. Each decision passes through a **hard-coded `RiskManager`** with 8 independent guards. Claude *cannot* override them.
8. Approved trades are executed via an async Hyperliquid SDK wrapper with retry/backoff.
9. Every action is logged to `diary.jsonl` and exposed via `/diary` and `/logs` REST endpoints.

### Hyperliquid-specific capabilities
- **229 + markets**: BTC/ETH/SOL + 200 other perps, plus HIP-3 tradfi (`xyz:TSLA`, `xyz:GOLD`, `xyz:SP500`, `xyz:EUR` …).
- **Agent wallet pattern**: an agent signer key that can trade but cannot withdraw — separates trading authority from custody.
- **Funding-aware positioning**: funding rates are treated as a tilt, not a trigger (must exceed ~0.25 × ATR edge).
- **On-chain settlement** via the official `hyperliquid-python-sdk`.

### The things that make it genuinely good
| # | Component | Why it's valuable |
|---|---|---|
| 1 | `risk_manager.py` | Clean, modular. Each guard returns `(ok, reason)`. 8 orthogonal checks: position size, total exposure, leverage, daily drawdown, concurrent positions, losing-position force-close, mandatory SL, balance reserve. |
| 2 | `decision_maker.py` | Production-grade Claude tool-calling pattern: structured JSON output, on-the-fly tool execution, Haiku fallback sanitizer, retries, full logging. |
| 3 | `local_indicators.py` | Pure-Python, dependency-free: EMA, SMA, RSI, MACD, ATR, BBands, ADX, OBV, VWAP, Stoch-RSI. Works on any OHLCV frame. |
| 4 | `HyperliquidAPI` + `_retry()` | Async wrapper with exponential backoff, thread-offload, connection-reset on failure. A template for any flaky exchange API. |
| 5 | LLM hysteresis / low-churn prompting | Prompt engineering that *reduces* LLM whipsaw: requires HTF + LTF confirmation to flip, enforces `cooldown_bars`, prefers stop-tightening over flipping. |
| 6 | Diary + reconciliation | Exchange-as-source-of-truth: local intent is purged if the exchange no longer has the position. |

---

## 2. What ProxiAlpha Currently Is

ProxiAlpha is a **multi-strategy, config-driven equity trading platform** with:

- **Broker**: Alpaca (stocks, paper + live). No crypto-derivatives. Data via `yfinance`.
- **14 strategies** behind a clean `BaseStrategy` ABC → `StrategyManager` consensus engine (weighted voting, STRONG_BUY..STRONG_SELL scale).
- **Backtester + paper-trader + live-trader** modes, CLI-driven via `main.py`.
- **FastAPI** backend + React web + React Native mobile + dashboard layer.
- **YAML-driven config** for strategies, risk, AI, watchlist, dashboard.
- **LLM adapter** (`core/llm_adapter.py`) with multi-provider support (Claude / OpenAI / Ollama / Gemini), and an `AIStrategy` that can inject signals into the consensus — but all **disabled by default** and essentially unused.
- **Custom pandas/numpy indicator math** inside `core/data_engine.py` (RSI, MACD, BB, ATR, SMAs, volume ratio, drawdown).

### ProxiAlpha's gaps (where Hyperliquid shines)
1. **No dedicated `RiskManager` class** — risk rules are smeared across YAML + backtester + paper trader.
2. **No crypto-derivatives broker** — can't touch perps, funding, leverage.
3. **LLM integration is a stub** — `AIStrategy` accepts injected JSON but has no tool-calling, no context-building, no sanitization, no cooldown/hysteresis logic.
4. **Indicators are ~90 reinvented lines** — more limited than Hyperliquid's set (no ADX, OBV, VWAP, Stoch-RSI).
5. **No diary / reconciliation loop** — local state can drift from broker state.
6. **Synchronous execution** — not async-first; will block under load.

---

## 3. What to Pull From Hyperliquid Into ProxiAlpha

There are six chunks of the Hyperliquid agent that port cleanly. I'm ordering them by **effort × impact** — do the first three regardless.

### Tier 1 — High impact, low effort (do these first)

#### A. Port `RiskManager` into `proxialpha/core/risk_manager.py`
**Copy from:** `hyperliquid-trading-agent/src/risk_manager.py`
**Target:** new file `proxialpha/core/risk_manager.py`

Refactor so the risk checks receive a generic `Portfolio` + `ProposedTrade` object instead of Hyperliquid-specific state. Replace `HyperliquidAPI.get_user_state()` with a `PortfolioProvider` protocol that both `alpaca_bot.py` and a future `hyperliquid_bot.py` can implement.

Then **call it from three places**:
- `backtesting/engine.py` before simulating a fill.
- `paper_trading/simulator.py` before opening/closing a simulated position.
- `live_trading/alpaca_bot.py` before `submit_order()`.

This instantly upgrades ProxiAlpha's risk enforcement from "YAML hints scattered everywhere" to "one authoritative gate every trade passes through." It's also the only way to safely turn on the dormant `AIStrategy`.

#### B. Port `local_indicators.py` into `proxialpha/core/indicators.py`
**Copy from:** `hyperliquid-trading-agent/src/indicators/local_indicators.py`
**Target:** replace the indicator math inside `proxialpha/core/data_engine.py`.

Adds ADX, OBV, VWAP, Stoch-RSI — none of which ProxiAlpha currently has. It also exposes a clean `compute_all(df)` and `last_n(indicator, n)` API that makes indicators addressable by name, which is a prerequisite for the tool-calling pattern in C.

#### C. Replace `ai_strategy.py` with the full Claude decision-maker pattern
**Copy from:** `hyperliquid-trading-agent/src/agent/decision_maker.py`
**Target:** rewrite `proxialpha/strategies/ai_strategy.py` (or create `proxialpha/core/ai_decision_maker.py`).

Bring over:
- The strict JSON output schema.
- The `fetch_indicator` tool definition — route it to the new `core/indicators.py`.
- The Haiku sanitizer fallback.
- The "low-churn / hysteresis / cooldown_bars" system prompt. This is *gold* for equity swing trading too — one of ProxiAlpha's biggest risks is LLM flip-flop between daily scans.
- The structured `exit_plan` field (ProxiAlpha's `StrategySignal` has `target_price` and `stop_loss` but no explicit *plan* with cooldown / invalidation conditions).

Wire the existing `core/llm_adapter.py` as the transport layer so multi-provider support is preserved (Claude / OpenAI / Ollama / Gemini).

### Tier 2 — Medium effort, high strategic value

#### D. Add a Hyperliquid broker adapter
**Copy from:** `hyperliquid-trading-agent/src/trading/hyperliquid_api.py`
**Target:** new file `proxialpha/live_trading/hyperliquid_bot.py`, sibling of `alpaca_bot.py`.

Implement the same surface area `alpaca_bot.py` exposes (`get_account`, `get_positions`, `submit_order`, `close_position`) so `StrategyManager` can route by asset class:
- Equities → Alpaca
- Perps / HIP-3 tradfi → Hyperliquid
- Spot crypto → whatever (future)

This unlocks:
- Crypto perps trading inside ProxiAlpha.
- Access to HIP-3 tradfi assets (stocks on-chain, 24/7, leveraged) — interesting overlap with the existing equity watchlist.
- The async `_retry()` helper becomes reusable by `alpaca_bot.py` too.

Add a new `config_trading.yaml` section:
```yaml
brokers:
  alpaca:
    enabled: true
    asset_classes: [equity]
  hyperliquid:
    enabled: false
    agent_wallet: ${HL_AGENT_KEY}
    vault_address: ${HL_VAULT}
    asset_classes: [perp, hip3]
```

#### E. Port the diary + reconciliation loop
**Copy from:** the relevant parts of `hyperliquid-trading-agent/src/main.py`
**Target:** `proxialpha/core/diary.py` + hooks into `paper_trading/simulator.py` and `live_trading/*`.

Every buy/sell/force-close/reconciliation writes a JSONL line. Expose through two new FastAPI endpoints in `api/server.py`: `GET /diary` and `GET /llm-logs`. This is cheap to add and makes debugging the AI strategy tractable, which matters the moment you turn it on.

### Tier 3 — Nice-to-have

#### F. Make the trading loop async-first
The Hyperliquid agent's `main.py` is a clean reference for an `asyncio` trading loop with a per-interval tick. ProxiAlpha's current CLI is synchronous; long-term you'll want this so `api/server.py` (FastAPI, already async) can run the live-trading loop in-process on a background task instead of shelling out to a separate process via `start.sh`.

---

## 4. Proposed File Layout After Integration

```
proxialpha/
├── core/
│   ├── data_engine.py          # keeps data fetching, delegates math to indicators.py
│   ├── indicators.py           # ← NEW, from hyperliquid local_indicators.py
│   ├── risk_manager.py         # ← NEW, from hyperliquid risk_manager.py
│   ├── ai_decision_maker.py    # ← NEW, from hyperliquid decision_maker.py
│   ├── diary.py                # ← NEW, trade/decision log
│   ├── llm_adapter.py          # unchanged — used as transport by ai_decision_maker
│   └── config.py
├── live_trading/
│   ├── alpaca_bot.py           # refactored to implement BrokerProtocol
│   ├── hyperliquid_bot.py      # ← NEW, from hyperliquid_api.py
│   └── broker_protocol.py      # ← NEW, abstracts broker surface area
├── strategies/
│   ├── ai_strategy.py          # now a thin wrapper over core/ai_decision_maker.py
│   └── ...existing 13 strategies untouched
└── api/
    └── server.py               # + /diary and /llm-logs endpoints
```

---

## 5. What NOT to Copy

- **The Hyperliquid-specific meta cache / HIP-3 DEX handling** — stays in `hyperliquid_bot.py`, doesn't belong in core.
- **The hard-coded 5 % mandatory stop-loss** — ProxiAlpha already has a more sophisticated ATR-based + trailing-stop system. Keep ProxiAlpha's, but plug it into the ported `RiskManager` as the `enforce_stop_loss` implementation.
- **The `hyperliquid-python-sdk` dependency** — make it an **optional extra** in `pyproject.toml` so users who only trade equities aren't forced to install it.
- **The monolithic `main.py`** — ProxiAlpha already has a richer CLI; don't replace it. Cherry-pick the async loop shape only.

---

## 6. Suggested Rollout Order

1. **Week 1** — Port `indicators.py` + `risk_manager.py`. Wire `risk_manager` into backtester + paper trader. Run existing backtests, confirm no regressions.
2. **Week 2** — Port `ai_decision_maker.py`. Wire to `llm_adapter.py`. Gate it behind `config_ai_integration.yaml` `require_human_approval: true` flag that's already there. Test on paper trading only.
3. **Week 3** — Add `diary.py` + the `/diary` and `/llm-logs` endpoints. Use them to debug the AI strategy's first live runs.
4. **Week 4** — Add `broker_protocol.py` and `hyperliquid_bot.py`. Start with paper-mode on Hyperliquid testnet.
5. **Week 5** — Route the existing equity watchlist strategies to Alpaca, and a new (small) crypto-perp strategy to Hyperliquid, behind the same `StrategyManager` consensus.

---

## TL;DR

The Hyperliquid agent is small but carries three **drop-in gems** ProxiAlpha is missing: a real `RiskManager`, a production-grade Claude tool-calling decision loop with JSON + sanitizer + hysteresis, and a richer indicator library. Porting those three (Tier 1) is a 1–2 week job, unblocks ProxiAlpha's dormant AI strategy, and leaves the door open to add Hyperliquid itself as a second broker (Tier 2) for crypto perps + HIP-3 tradfi — without touching ProxiAlpha's 14-strategy consensus engine, which is already the right place to plug it all in.
