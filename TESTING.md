# ProxiAlpha Usage & Testing Guide

A practical, layered guide to **running** and **testing** the ProxiAlpha
framework — with special attention to the pieces added during the
Hyperliquid integration (RiskManager, diary, BrokerProtocol /
BrokerRouter, Hyperliquid adapter, AIStrategy wrapper, and the new
`/api/diary` · `/api/llm-logs` · `/api/risk/summary` endpoints).

The guide has three parts:

- **Part A — Running the platform** (§A1–A7): install, start, modes,
  first-run checklist, troubleshooting.
- **Part B — Testing the platform** (§B0–B6): the 8-step test ladder,
  smoke runner, manual live rungs, and recommended workflow.
- **Part C — Local LLM on DGX** (§C1–C8): install Ollama on a DGX /
  multi-GPU server, expose it to ProxiAlpha, model selection by VRAM,
  vLLM alternative for high throughput, and smoke testing.

If a rung fails, **fix it before moving up**.

---

# Part A — Running the platform

## A1. Install (once)

From the repo root:

```bash
cd proxiant/proxialpha
./install.sh                # basic install (uv or pip, creates ./venv)
./install.sh --with-claude  # add Anthropic SDK for the AIStrategy
./install.sh --with-openai  # add OpenAI SDK
./install.sh --with-gemini  # add Google Gemini SDK
./install.sh --with-alpaca  # add Alpaca live trading SDK
./install.sh --with-all     # everything (all LLM providers + Alpaca)
./install.sh --pip          # force plain pip (skip uv even if available)
```

The installer creates a virtualenv at `./venv`, installs
`requirements.txt`, writes a lock file, optionally offers to install
Ollama for local LLMs, and runs a verification pass at the end. If `uv`
isn't installed it'll offer to install it.

For Hyperliquid specifically (not covered by `--with-all`):

```bash
source venv/bin/activate
pip install '.[hyperliquid]'
```

## A2. Verify the install

Before touching real money or real keys, make sure the smoke test is
green (this is Part B in one command):

```bash
bash scripts/smoke.sh
```

Should end in `passed: 7 / failed: 0 / ALL GREEN`.

## A3. Start the platform

The normal way — start the API server and dashboard:

```bash
./start.sh
```

That opens:

- **Dashboard** — `http://localhost:8000`
- **API docs (Swagger)** — `http://localhost:8000/docs`
- **WebSocket signal feed** — `ws://localhost:8000/ws/signals`

New endpoints from the Hyperliquid integration are already wired in at:

- `http://localhost:8000/api/risk/summary`
- `http://localhost:8000/api/diary?source=paper&limit=20`
- `http://localhost:8000/api/llm-logs?n_bytes=5000`

Ctrl+C to stop.

Useful `start.sh` flags:

```bash
./start.sh --port 9000         # custom port
./start.sh --host 127.0.0.1    # localhost only (default binds 0.0.0.0)
./start.sh --with-ollama       # boot Ollama first, then the API server
./start.sh --install-service   # install a systemd unit (Linux only)
```

## A4. Run modes (execute and exit)

These use `main.py` directly and don't start the server — useful for cron,
CI, or one-off analysis:

```bash
./start.sh --scan         # scan watchlist for signals, print, exit
./start.sh --backtest     # historical backtest, print summary, exit
./start.sh --paper        # run paper-trading simulator
./start.sh --ai-signals   # AI-powered signals (needs Claude/OpenAI key)
./start.sh --excel        # generate Excel tracker
./start.sh --all          # scan + backtest + paper + excel
```

Equivalent direct invocation if you'd rather not use `start.sh`:

```bash
source venv/bin/activate
python main.py --mode scan
python main.py --mode backtest --capital 100000
python main.py --mode paper
python main.py --mode ai-signals
python main.py --mode excel
python main.py --mode all
```

## A5. Safe first-run checklist

Follow this top to bottom on a fresh clone. Do not skip steps.

1. `./install.sh --with-claude` (or `--with-all` if you want every SDK).
2. `bash scripts/smoke.sh` → all green.
3. Edit `config_trading.yaml`:
   - `brokers.alpaca.enabled: true` (paper mode)
   - `brokers.hyperliquid.enabled: false` (flip on later)
   - Confirm `risk_manager:` block is present with sensible defaults
     (`max_position_pct: 10.0`, `mandatory_sl_pct: 8.0`, etc.).
4. Edit `config_ai_integration.yaml`:
   - `use_api: false` for the very first run (safe-no-LLM mode).
   - `decision_maker.cooldown_bars` ≥ 3 for low-churn behavior.
5. `./start.sh --scan` → see signals without touching any money.
6. `./start.sh --backtest` → verify engine + RiskManager interplay on
   historical data. Check `data/backtest_diary.jsonl` for
   `trade_submitted` / `trade_executed` / `trade_rejected` entries.
7. `./start.sh` → launch the dashboard. Hit:
   - `http://localhost:8000/api/risk/summary`
   - `http://localhost:8000/api/diary?source=backtest&limit=20`
8. Only after all of the above looks healthy: set
   `ANTHROPIC_API_KEY`, flip `use_api: true` in
   `config_ai_integration.yaml`, and run
   `./start.sh --ai-signals` to smoke-test the live LLM path.
   Tail `data/llm_requests.log` or hit `/api/llm-logs` to watch the
   decision maker's prompts, tool calls, and sanitized JSON decisions.
9. Broker keys — **only after steps 1–8 pass**:
   - **Alpaca paper**: `export APCA_API_KEY_ID=PK...` and
     `export APCA_API_SECRET_KEY=...`.
   - **Hyperliquid testnet**: `export HL_AGENT_KEY=0x...` (agent wallet),
     `export HL_VAULT=0x...` (optional), set
     `brokers.hyperliquid.network: testnet` and
     `brokers.hyperliquid.enabled: true` in `config_trading.yaml`.
   - Read-only smoke first (see §B3.1 / §B3.2). Only submit a live order
     after `get_account` / `get_positions` / `get_candles` all return
     correct data on the testnet.

## A6. Platform startup troubleshooting

**`./start.sh` says "Virtual environment not found"**
Run `./install.sh` first.

**Imports fail after install**
`source venv/bin/activate && python scripts/test_imports.py` → prints
exactly which module failed.

**Port already in use**
`./start.sh --port 8001` (or kill the process on 8000).

**Dashboard loads but shows no data**
You haven't run `--scan` or `--backtest` yet; the diary is empty.
Run `./start.sh --backtest` first, then refresh.

**`AIStrategy` does nothing**
Check `config_ai_integration.yaml → use_api`. If `false`, the strategy
is in safe-no-LLM mode and will return HOLD every time. That's intentional
for the first run.

**Hyperliquid bot fails to init**
Expected unless `pip install '.[hyperliquid]'` has been run **and**
`HL_AGENT_KEY` is in the environment. Equity-only workflows don't need it
— `HyperliquidLiveTrader._init_api()` returns `False` with a warning and
the rest of the system keeps working.

**systemd service (Linux)**
`./start.sh --install-service` writes `/etc/systemd/system/proxialpha.service`
and starts it. Inspect with:

```bash
sudo systemctl status proxialpha
journalctl -u proxialpha -f
```

## A7. Quickstart (TL;DR)

```bash
cd proxiant/proxialpha
./install.sh --with-claude
bash scripts/smoke.sh
./start.sh
# open http://localhost:8000
```

---

# Part B — Testing the platform

## B0. One-shot smoke runner

Everything described below is wired into a single runner:

```bash
cd proxiant/proxialpha
bash scripts/smoke.sh
```

Expected tail:

```
==============================================================
  SMOKE TEST RESULTS
==============================================================
  passed: 7
  failed: 0
  ALL GREEN
```

`smoke.sh` chains 7 steps in order. It exits non-zero at the first failure
and prints the list of failed steps at the end. The 7th step (AIStrategy)
auto-skips unless `ANTHROPIC_API_KEY` is set.

---

## B1. Prerequisites

Core deps (required for the smoke runner):

```bash
pip install --break-system-packages \
    pandas numpy pyyaml yfinance \
    fastapi uvicorn pydantic httpx
```

Optional AI deps (required for step 7):

```bash
pip install --break-system-packages anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

Optional Hyperliquid deps (required only for live HL calls — step 8):

```bash
pip install --break-system-packages '.[hyperliquid]'
```

---

## B2. The test ladder

Each step has a dedicated script under `scripts/`. Scripts are
self-contained and exit non-zero on failure.

### Step 1 — Import smoke test

```bash
python scripts/test_imports.py
```

Imports every integrated module: core, strategies, backtesting, paper
trading, live trading, API. Fails loudly on any syntax or circular-import
error. **Takes under a second.**

### Step 2 — RiskManager unit checks

```bash
python scripts/test_risk.py
```

Pure-function tests that verify the four critical guards:

1. **Allocation cap** — $50k request on a $100k account → capped to $10k
   (10% default).
2. **Auto stop-loss** — BUY with no SL → auto-set 8% below entry.
3. **Min order bump** — $50 request → bumped up to `min_order_usd` ($100),
   not rejected.
4. **Concurrent limit** — 10 positions already open → 11th BUY rejected.
5. **Sell passthrough** — SELL orders bypass allocation checks.

No network, no dataframes. Takes milliseconds.

### Step 3 — Broker routing

```bash
python scripts/test_routing.py
```

Uses a tiny in-file `StubBroker` to verify `BrokerRouter.classify_ticker()`
and `BrokerRouter.for_ticker()` route correctly without touching any real
exchange SDK:

| Input     | Class  | Target       |
|-----------|--------|--------------|
| `AAPL`    | equity | alpaca-stub  |
| `MSFT`    | equity | alpaca-stub  |
| `BTC`     | perp   | hl-stub      |
| `ETH`     | perp   | hl-stub      |
| `xyz:TSLA`| hip3   | hl-stub      |

Also asserts both stubs satisfy `BrokerProtocol` via `isinstance()` (the
protocol is `runtime_checkable`).

### Step 4 — Backtest with risk gate wired in

```bash
python scripts/test_backtest.py
```

Uses the existing `demo_data` generator (no network), loads two strategies
into a `StrategyManager`, and runs `BacktestEngine` with
`risk_manager=RiskManager()` and `enable_diary=True`. Verifies:

- The engine retained the injected RiskManager.
- The engine's RiskManager parameters match (`max_position_pct`,
  `min_order_usd`).
- `data/backtest_diary.jsonl` is writable (entry count printed).

Note: with pure synthetic demo data and baseline strategies, the backtest
may produce zero trades — that's expected baseline behavior, not a
regression. This step is checking wiring, not performance.

### Step 5 — Paper trader + risk + diary

```bash
python scripts/test_paper.py
```

Creates a `PaperTrader` with a temp state file and calls `execute_buy` /
`execute_sell` directly:

- **Oversized buy** ($50k on a $100k account, AAPL@$150) → capped to ~$10k
  (66 shares); logs `trade_submitted` / `trade_executed` to
  `data/paper_diary.jsonl`.
- **Zero-share buy** ($50 on $300 ticker) → rejected before risk gate.
- **Sell without position** → rejected by the simulator.

### Step 6 — API endpoints

```bash
python scripts/test_api.py
```

Uses FastAPI's `TestClient` (in-process, no uvicorn, no port). Hits all
three new endpoints:

- `GET /api/risk/summary` → asserts `risk.max_position_pct` present.
- `GET /api/llm-logs?n_bytes=1024` → asserts `log` + `path` keys.
- `GET /api/diary?source={paper,live,backtest,ai}&limit=5` → asserts 200
  and a JSON list (possibly empty).

Skips gracefully with exit code 0 if `fastapi[testclient]` / `httpx` are
not installed.

### Step 7 — AIStrategy end-to-end (optional)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/test_ai.py
```

Builds a synthetic 120-day OHLCV DataFrame, instantiates `AIStrategy` with
`use_api=True, enable_tools=True`, and calls `generate_signals()`. Verifies
the returned `StrategySignal` list has valid `SignalType` values and
confidences in `[0,1]`. **Skips (exit 0) if `ANTHROPIC_API_KEY` is unset.**

---

## B3. Manual / live rungs (not in smoke.sh)

These need real credentials and hit real servers. Run manually when you
promote the framework from development to live operation.

### B3.1 Alpaca paper trading (equities)

```bash
export APCA_API_KEY_ID=PK...
export APCA_API_SECRET_KEY=...
python -c "
from live_trading.alpaca_bot import AlpacaLiveTrader
t = AlpacaLiveTrader(paper=True)
print(t.get_account())
print(t.get_positions())
print(t.get_candles('AAPL', '1D', 5)[:2])
"
```

You should see a non-zero `portfolio_value` and a JSON-shaped account
dict. **Do not submit any live orders** until `get_account` / `get_positions`
return correct values.

### B3.2 Hyperliquid testnet (perps + HIP-3)

1. Install the extra:
   ```bash
   pip install --break-system-packages '.[hyperliquid]'
   ```
2. In `config_trading.yaml` set:
   ```yaml
   brokers:
     hyperliquid:
       enabled: true
       network: testnet
   ```
3. Create an agent wallet (Hyperliquid docs → Agent Wallets) and export its
   private key:
   ```bash
   export HL_AGENT_KEY=0x...
   export HL_VAULT=0x...   # optional
   ```
4. Read-only smoke (no orders):
   ```bash
   python -c "
   from live_trading.hyperliquid_bot import HyperliquidLiveTrader
   t = HyperliquidLiveTrader()
   print(t.get_account())
   print(t.get_positions())
   print(t.get_candles('BTC','1h',5)[:2])
   "
   ```
5. Only after all three of the above return correct data, try a small
   test order (e.g. $11 BTC perp at market) on **testnet**, verify the
   fill on the HL UI, then `close_position('BTC')` to flatten.
6. When you're confident, flip `network: mainnet` and repeat with a tiny
   position before scaling.

### B3.3 Live API server

```bash
uvicorn api.server:app --reload --port 8000
```

From another shell:

```bash
curl localhost:8000/api/risk/summary
curl "localhost:8000/api/diary?source=paper&limit=20"
curl "localhost:8000/api/diary?source=backtest&event=trade_rejected"
curl "localhost:8000/api/llm-logs?n_bytes=5000"
```

### B3.4 Full pipeline

```bash
./start.sh             # or: python main.py
```

See §A3–A5 for the full `start.sh` flag list and the first-run
checklist. For the first live bars, keep `use_api: false` in
`config_ai_integration.yaml` so the AI strategy runs in safe-no-LLM mode.
Once you're happy with the diary output, flip to `use_api: true` and
monitor `/api/llm-logs` to watch the decision maker's prompts, tool calls,
and sanitized JSON decisions.

---

## B4. What each file is for

| File                           | Purpose                                                          |
|--------------------------------|------------------------------------------------------------------|
| `install.sh`                   | Bootstrap the venv, deps, optional LLM SDKs, and Ollama.        |
| `start.sh`                     | Launch the API server or a one-shot run mode.                   |
| `main.py`                      | Direct CLI entry point (`--mode scan/backtest/paper/ai-signals/excel/all`). |
| `scripts/smoke.sh`             | One-shot test runner, chains all 8 steps, prints PASS/FAIL.     |
| `scripts/test_imports.py`      | Step 1 — every module imports cleanly.                          |
| `scripts/test_risk.py`         | Step 2 — RiskManager guards behave as specified.                |
| `scripts/test_routing.py`      | Step 3 — BrokerRouter picks the right adapter per ticker class. |
| `scripts/test_backtest.py`     | Step 4 — backtester respects the injected RiskManager + diary.  |
| `scripts/test_paper.py`        | Step 5 — PaperTrader gate works, diary is written.              |
| `scripts/test_api.py`          | Step 6 — `/api/risk/summary`, `/api/llm-logs`, `/api/diary` up. |
| `scripts/test_ai.py`           | Step 7 — AIStrategy live LLM call (skipped w/o `ANTHROPIC_API_KEY`). |
| `scripts/test_ollama.py`       | Step 8 — LLMAdapter → Ollama round-trip (skipped w/o `OLLAMA_BASE_URL`). |
| `test_platform.py`             | Legacy platform regression test (still runs fine).              |
| `config_trading.yaml`          | Brokers (Alpaca / Hyperliquid), `risk_manager:` block, backtest defaults. |
| `config_ai_integration.yaml`   | AI decision maker: model, tools, cooldown, LLM log path.        |
| `config_strategies.yaml`       | Strategy weights and per-strategy params.                       |
| `config_watchlist.yaml`        | Tickers scanned by `main.py --mode scan` and the live loop.     |

---

## B5. Test-layer troubleshooting

For platform startup issues (venv missing, port in use, dashboard empty),
see §A6. This section covers failures in the smoke test itself.

**"No module named 'yfinance'" / pandas / pyyaml**
Install the core deps in §B1.

**`/api/diary` 404 or empty**
Run step 5 (paper) or step 4 (backtest) first to create entries.
Verify the file at `data/paper_diary.jsonl` or `data/backtest_diary.jsonl`.

**`ModuleNotFoundError: hyperliquid`**
Expected unless you've installed the optional extra. Equity-only workflows
don't need it — `HyperliquidLiveTrader._init_api()` returns `False` with a
warning and the rest of the system keeps working.

**`TypeError: validate_trade() missing 1 required positional argument: 'initial_balance'`**
You're calling the RiskManager directly. Always pass the starting capital
as the third positional arg — the backtester and paper trader do this
automatically.

**AIStrategy returns empty signals**
The cooldown hysteresis intentionally returns HOLD until
`cooldown_bars` have passed since the last signal. Set
`cooldown_bars: 0` in `config_ai_integration.yaml → decision_maker:` for
smoke testing only; restore it before going live.

**Backtest reports 0 trades**
This is expected baseline behavior with the current synthetic demo data and
vanilla DipBuyer / Technical strategies. Step 4 tests *wiring*, not PnL.
For performance testing, load real data via `fetch_stock_data()` in a
notebook and compare against a buy-and-hold baseline.

---

## B6. Recommended workflow

Day-to-day:

```bash
bash scripts/smoke.sh     # 5–10 seconds, run before every commit
```

Before deploying code changes to a live session:

```bash
bash scripts/smoke.sh
python test_platform.py   # legacy full-demo regression
# plus a manual read-only hit on §B3.1 / §B3.2 if you touched broker adapters
```

Before flipping `use_api: true` on the AIStrategy:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python scripts/test_ai.py
# then: tail -f data/llm_requests.log  (or hit /api/llm-logs)
```

That's it. Eight fast steps (step 8 auto-skips without
`OLLAMA_BASE_URL`), one shell command, green-or-red output — then
follow §A5 to promote from smoke-tested to running.

---

# Part C — Local LLM on DGX

This part covers running ProxiAlpha's LLM calls against a **local** or
**remote** Ollama server — most commonly on an NVIDIA DGX box (A100 or
H100, 40–640 GB VRAM). A DGX has enough headroom to run 70B+ models
at high throughput, which beats most cloud API options for latency and
cost once you own the hardware.

## C1. What works with Ollama (and what doesn't)

ProxiAlpha's `core/llm_adapter.py` already supports three provider
strings that are relevant here:

| Provider     | Backend                     | Tool calling | Use case                          |
|--------------|-----------------------------|--------------|-----------------------------------|
| `ollama`     | Ollama HTTP API :11434       | ❌ (JSON mode only) | Signal gen, analysis, risk commentary |
| `custom`     | Any OpenAI-compatible server | ✅ (if model/server supports it) | vLLM, TGI, LMDeploy, LM Studio |
| `claude`     | Anthropic SDK                | ✅ (native)  | `AIDecisionMaker` tool-calling loop |

**Important caveat.** The `AIDecisionMaker` (`core/ai_decision_maker.py`)
— the production tool-calling loop with `fetch_indicator` and the Haiku
sanitizer — is **Anthropic-specific**. Ollama's native API doesn't
expose Anthropic-style tool calling, so you can't move the decision
maker onto Ollama without a rewrite.

On a DGX, use Ollama for:

- `integration_modes.signal_generation`
- `integration_modes.market_analysis`
- `integration_modes.strategy_optimization`
- `integration_modes.risk_monitor`
- Any direct `LLMAdapter.generate(...)` calls in your own code

Keep `decision_maker.enabled: true` pointed at Claude, or graduate to
vLLM + a tool-calling-capable model (see §C7).

## C2. Install Ollama on the DGX

Ollama binds CUDA on startup, so no separate GPU config is needed as
long as the NVIDIA driver is healthy.

```bash
# On the DGX itself
nvidia-smi                                   # confirm all GPUs visible
curl -fsSL https://ollama.com/install.sh | sh
systemctl status ollama                      # installer registers a systemd unit
ollama --version
```

**Verify Ollama is using the GPUs** (not silently falling back to CPU):

```bash
journalctl -u ollama -n 100 | grep -iE "gpu|cuda|compute"
# Expect lines like:
#   msg="inference compute" id=GPU-xxxx library=cuda compute=9.0
```

If you see `library=cpu`, the driver / CUDA link is broken — fix that
before pulling any models. A DGX should never be on CPU fallback.

## C3. Expose Ollama to ProxiAlpha

**Case A — ProxiAlpha runs on the same DGX** (simplest):
default `http://localhost:11434` works. Nothing to do.

**Case B — ProxiAlpha runs on another host** (typical:
dev laptop or app server, DGX is inference-only):

```bash
# On the DGX
sudo systemctl edit ollama
```

Add a drop-in with DGX-appropriate tuning:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama

# Lock the port down to the ProxiAlpha host(s) only — Ollama has no auth
sudo ufw allow from <proxialpha-host> to any port 11434
```

Sanity check from the ProxiAlpha host:

```bash
curl http://dgx.local:11434/api/tags
```

**Tuning knobs that matter on DGX:**

- `OLLAMA_KEEP_ALIVE=30m` — keep models resident in VRAM between
  requests. DGX has the VRAM, use it.
- `OLLAMA_NUM_PARALLEL=4` — serve 4 concurrent requests per model
  (ProxiAlpha fans out across watchlist tickers).
- `OLLAMA_MAX_LOADED_MODELS=2` — useful if you want a big decision model
  **and** a cheap sanitizer model loaded at once.
- `CUDA_VISIBLE_DEVICES=0,1` — pin Ollama to specific GPUs if other
  workloads share the box.
- `OLLAMA_SCHED_SPREAD=1` — on multi-GPU DGX, spread layers across GPUs
  (useful for 70B models that don't fit on one card even at 80GB).

## C4. Pull models sized for DGX VRAM

Don't run `llama3.1:8b` on a DGX — that leaves 95% of the hardware idle.
Recommended models by VRAM tier:

| Hardware          | Recommended models                                                     |
|-------------------|-----------------------------------------------------------------------|
| 1× A100 40GB      | `llama3.1:70b-instruct-q4_K_M`, `qwen2.5:72b-instruct-q4_K_M`, `mixtral:8x7b` |
| 1× A100 80GB      | `llama3.1:70b-instruct-q8_0`, `qwen2.5:72b-instruct-q5_K_M`, `deepseek-r1:70b` |
| 1× H100 80GB      | same as A100 80GB but 2–3× the throughput                              |
| 4–8× H100         | `llama3.1:405b-instruct-q4_K_M`, `deepseek-v3`, `qwen2.5:72b-instruct-q8_0` |

70B is the sweet spot for trading analysis — strong reasoning, fits on a
single 80 GB card, fast enough for a live scan loop.

```bash
ollama pull llama3.1:70b-instruct-q8_0
ollama pull qwen2.5:32b-instruct            # smaller fast option
ollama pull mistral-nemo:12b                # cheap sanitizer / fallback
ollama list                                  # confirm downloads
ollama ps                                    # what's currently resident in VRAM
```

**Throughput sanity check** — warm the model then time a single call:

```bash
time curl -s http://localhost:11434/api/generate \
  -d '{"model":"llama3.1:70b-instruct-q8_0","prompt":"Say hi.","stream":false}' \
  | jq -r '.response, .eval_count, .eval_duration'
```

On a single H100 80GB you should see **30–60 tokens/sec** for 70B at q8.
Under 10 tok/s means something is wrong (probably CPU fallback — go back
to §C2).

## C5. Point ProxiAlpha at Ollama

**Option 1 — Config-driven** (recommended). Add a `local_llm:` block to
`config_ai_integration.yaml`:

```yaml
local_llm:
  enabled: true
  provider: "ollama"                        # or "custom" for vLLM/TGI
  base_url: "http://dgx.local:11434"        # or http://localhost:11434
  model: "llama3.1:70b-instruct-q8_0"
  temperature: 0.3
  max_tokens: 2048
  timeout_seconds: 120                      # 70B first-token can be slower
  system_prompt: "You are a quantitative trading analyst."
```

**Option 2 — Environment variables** (works immediately, no config
edits — these are also what `scripts/test_ollama.py` reads):

```bash
export OLLAMA_BASE_URL=http://dgx.local:11434
export OLLAMA_MODEL=llama3.1:70b-instruct-q8_0
export OLLAMA_TIMEOUT=120
```

Then anywhere in your code:

```python
from core.llm_adapter import LLMAdapter

llm = LLMAdapter(
    provider="ollama",
    model="llama3.1:70b-instruct-q8_0",
    base_url="http://dgx.local:11434",
    timeout=120,
    max_tokens=2048,
    temperature=0.3,
)
response = llm.generate("Analyze AAPL at $185 with RSI 28 and 4% below 50d MA.")
print(response.text)
print(response.usage)   # {'eval_count': ...}
```

## C6. Smoke test

```bash
export OLLAMA_BASE_URL=http://dgx.local:11434
export OLLAMA_MODEL=llama3.1:70b-instruct-q8_0
python scripts/test_ollama.py
```

This is step 8 in the main smoke runner:

```bash
bash scripts/smoke.sh
```

`test_ollama.py` does four things:

1. Hits `/api/tags` on the Ollama server and prints the loaded models.
2. Checks the target model is in the list (warns but continues if not —
   Ollama can pull on demand).
3. Runs a round-trip through `LLMAdapter(provider="ollama", ...)` with a
   deterministic "reply with `pong`" prompt.
4. Asserts response text is non-empty and `eval_count > 0`.

If `OLLAMA_BASE_URL` is unset, the step auto-skips with exit 0, so it's
safe to leave enabled in `smoke.sh` all the time.

Also hit the running dashboard to confirm ProxiAlpha is talking to the
DGX end-to-end:

```bash
./start.sh --ai-signals        # runs main.py --mode ai-signals
curl localhost:8000/api/llm-logs?n_bytes=5000
```

You should see the DGX's responses (and token counts) in the log.

## C7. vLLM alternative for high throughput

Ollama is convenient but optimized for single-user laptop use. For a DGX
with serious concurrency needs, **vLLM** typically delivers **5–10×
higher throughput** (continuous batching, paged attention, tensor
parallelism) and it exposes an **OpenAI-compatible** endpoint so
ProxiAlpha can hit it via `provider="custom"`:

```bash
# On the DGX
pip install vllm
vllm serve meta-llama/Llama-3.1-70B-Instruct \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size 4 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.90
```

Then in ProxiAlpha:

```python
llm = LLMAdapter(
    provider="custom",
    model="meta-llama/Llama-3.1-70B-Instruct",
    base_url="http://dgx.local:8000/v1",
)
```

vLLM also supports real function/tool calling for several model
families, which is the path forward if you want to port the
`AIDecisionMaker` off Anthropic onto a local model entirely
(Tier 3 in the original Hyperliquid integration plan).

## C8. DGX-specific gotchas

- **MIG partitioning** — if the DGX is carved into MIG slices, Ollama
  only sees the slice you've exposed to it. Run `nvidia-smi -L` and
  confirm full GPUs are visible before pulling a 70B+ model.
- **NVLink** — for 70B/405B models split across multiple GPUs (Ollama
  with `OLLAMA_SCHED_SPREAD=1`, or vLLM with `--tensor-parallel-size`),
  NVLink gives 5–10× the inter-GPU bandwidth of PCIe. The DGX has it;
  make sure your container runtime isn't hiding it.
  `nvidia-smi topo -m` should show `NV#` links.
- **Docker / Slurm** — if you run Ollama in a container
  (`ollama/ollama:latest`), use `--gpus all` and mount `/root/.ollama` as
  a volume so pulled models survive restarts.
- **Firewall / auth** — Ollama binds 11434 with **no authentication**.
  On a shared DGX, restrict inbound to your ProxiAlpha host only, or
  front it with an auth proxy / Tailscale.
- **Shared DGX etiquette** — set `CUDA_VISIBLE_DEVICES` and
  `OLLAMA_MAX_LOADED_MODELS=1` so you don't hog VRAM other jobs need.
- **Concurrency vs latency** — ProxiAlpha's scan path fires one LLM call
  per ticker. For a 20-symbol watchlist, either raise
  `OLLAMA_NUM_PARALLEL` or move to vLLM; otherwise scans serialize and
  take a while.
- **First-token latency** — Ollama loads the model on first request
  after `OLLAMA_KEEP_ALIVE` expires. Expect a 10–30s cold start on 70B.
  The `test_ollama.py` smoke uses a warm call; if you're building a
  live loop, pre-warm with a dummy request at startup.

## C9. Quickstart (TL;DR)

```bash
# On DGX
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama     # add OLLAMA_HOST=0.0.0.0:11434, KEEP_ALIVE=30m, NUM_PARALLEL=4
sudo systemctl restart ollama
ollama pull llama3.1:70b-instruct-q8_0

# On ProxiAlpha host
cd proxiant/proxialpha
./install.sh
export OLLAMA_BASE_URL=http://dgx.local:11434
export OLLAMA_MODEL=llama3.1:70b-instruct-q8_0
bash scripts/smoke.sh          # step 8 now runs the DGX round-trip
./start.sh --ai-signals        # live scan routed through DGX
```
