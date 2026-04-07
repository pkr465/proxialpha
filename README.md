# ProxiAlpha

ProxiAlpha — AI-powered multi-strategy trading platform. Hosted at [proxiant.ai/proxialpha](https://proxiant.ai/proxialpha.html)

## Quick Start

```bash
# One-command install (Python deps + optional Ollama)
./install.sh

# Start the platform
./start.sh
```

That's it. Dashboard is at `http://localhost:8000`, web terminal at [proxiant.ai/terminal](https://proxiant.ai/terminal.html).

## Install

### Option A: Automated (recommended)

```bash
git clone <your-repo-url> ~/proxialpha
cd ~/proxialpha
./install.sh
```

The installer will:
- Detect Python 3.10+ and create a virtual environment
- Install all pip dependencies
- Generate `requirements.txt`
- Optionally install Ollama with GPU-aware model recommendations
- Verify everything works

### Install Options

```bash
./install.sh                    # Auto-detect uv or pip, core deps only
./install.sh --with-claude      # + Anthropic Claude SDK
./install.sh --with-openai      # + OpenAI SDK
./install.sh --with-all-llm     # + All LLM provider SDKs
./install.sh --with-all         # + All optional deps (LLMs + Alpaca)
./install.sh --pip              # Force pip (skip uv even if available)
```

The installer will prompt to install [uv](https://docs.astral.sh/uv/) (10-100x faster than pip). Say yes for faster installs, or use `--pip` to stick with pip.

### Option B: Manual

```bash
git clone <your-repo-url> ~/proxialpha
cd ~/proxialpha

# With uv (fast)
uv venv venv
source venv/bin/activate
uv pip install -r requirements.txt

# Or with pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Start

### `start.sh` Reference

```bash
./start.sh                    # API server on port 8000
./start.sh --with-ollama      # Start Ollama + API server
./start.sh --port 9000        # Custom port
./start.sh --scan             # Run signal scan then exit
./start.sh --backtest         # Run backtest then exit
./start.sh --ai-signals       # AI-powered signal generation
./start.sh --all              # Full analysis (scan + backtest + paper + excel)
./start.sh --install-service  # Install systemd service (Linux/DGX)
```

### Manual Start

```bash
source venv/bin/activate

# API server
uvicorn api.server:app --host 0.0.0.0 --port 8000

# CLI modes
python main.py --mode scan          # Scan watchlist for signals
python main.py --mode backtest      # Run historical backtest
python main.py --mode paper         # Paper trading simulator
python main.py --mode live          # Live trading via Alpaca
python main.py --mode ai-signals    # LLM signal generation
python main.py --mode excel         # Generate Excel tracker
python main.py --mode all           # Run everything
```

## Install on DGX Station (GPU + Local LLM)

Run ProxiAlpha on a DGX station with Ollama for fully local, private LLM inference — no API keys, no cloud, no data leaving your machine.

### Prerequisites

- NVIDIA DGX Station (or any Linux machine with NVIDIA GPUs)
- CUDA drivers installed
- Python 3.10+

### Step 1: Install

```bash
ssh user@your-dgx-ip

git clone <your-repo-url> ~/proxialpha
cd ~/proxialpha
./install.sh
```

The installer detects your GPU and recommends models. Say **yes** when prompted to install Ollama and pull a model.

### Step 2: Configure Ollama as the LLM Provider

Edit `config_ai_integration.yaml`:

```yaml
anthropic:
  api_key: null   # not needed for Ollama

ollama:
  base_url: "http://localhost:11434"
  model: "llama3.1:70b"           # or llama3.1:8b for less VRAM
  model_analysis: "llama3.1:70b"
  timeout_seconds: 120            # larger models need more time
```

Enable AI in `config_strategies.yaml`:

```yaml
AI_Claude:
  active: true
  weight: 1.5
  provider: "ollama"
```

### Step 3: Start

```bash
# Option A: One command (starts Ollama + API server)
./start.sh --with-ollama

# Option B: Separate terminals
ollama serve                    # Terminal 1
./start.sh                      # Terminal 2
./start.sh --ai-signals         # Terminal 3 (optional: run a scan)
```

Dashboard is at `http://YOUR_DGX_IP:8000`.

### Step 4: Run as a System Service

```bash
./start.sh --install-service
```

This creates and starts a systemd service that auto-restarts on boot. Manage it with:

```bash
sudo systemctl status proxialpha      # Check status
journalctl -u proxialpha -f           # Follow logs
sudo systemctl restart proxialpha     # Restart
sudo systemctl stop proxialpha        # Stop
```

### Step 5: Optional — HTTPS with Nginx

If your DGX has a public hostname (e.g. `dgx.proxiant.ai`):

```bash
sudo apt install nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/proxialpha`:

```nginx
server {
    server_name dgx.proxiant.ai;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/proxialpha /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dgx.proxiant.ai
```

Then update `terminal.html` on the proxiant.ai repo:

```js
const API_BASE = window.PROXIALPHA_API || "https://dgx.proxiant.ai";
```

### Recommended Models by GPU

| GPU VRAM | Model | Use Case |
|----------|-------|----------|
| 8 GB | `llama3.1:8b` | Fast signals, basic analysis |
| 24 GB | `llama3.1:8b` + `deepseek-coder` | Multi-model consensus |
| 40 GB | `llama3.1:70b` | Full analysis, strategy optimization |
| 80 GB+ | `llama3.1:70b` + `mixtral:8x22b` | Maximum quality, parallel inference |

## Architecture

```
proxialpha/
├── core/                       # Data engine, config, demo data
├── strategies/                 # Modular strategy plugins
│   ├── base.py                 # BaseStrategy interface
│   ├── dip_buyer.py            # Buy at pullback levels
│   ├── technical.py            # RSI, MACD, MA scoring
│   ├── momentum.py             # ROC + relative strength
│   ├── mean_reversion.py       # Z-score + Bollinger Bands
│   ├── breakout.py             # Volume-confirmed breakouts
│   ├── trend_following.py      # Multi-TF MA + ADX
│   ├── dca.py                  # Dollar-cost averaging
│   ├── pairs_trading.py        # Statistical arbitrage
│   ├── earnings_play.py        # Earnings drift
│   ├── sector_rotation.py      # Sector momentum
│   ├── scalping.py             # Micro reversals
│   ├── swing_trading.py        # S/R + Fibonacci
│   ├── options_flow.py         # Unusual volume detection
│   ├── custom_rules.py         # JSON rule engine
│   ├── ai_strategy.py          # LLM-powered signal plugin
│   └── strategy_manager.py     # Orchestrator + consensus
├── backtesting/                # Historical backtesting engine
├── paper_trading/              # Paper trading simulator
├── live_trading/               # Alpaca API live trading
├── api/                        # FastAPI REST + WebSocket server
├── web/                        # React dashboard (index.html, app.jsx)
├── config_strategies.yaml      # Strategy parameters + weights
├── config_watchlist.yaml       # Stock universe + thesis
├── config_trading.yaml         # Execution + risk rules
├── config_ai_integration.yaml  # LLM provider settings
├── config_dashboard.yaml       # Dashboard layout config
├── requirements.txt            # Pinned Python dependencies
├── pyproject.toml              # Project metadata + optional extras
├── install.sh                  # Automated installer (uv/pip + Ollama)
├── start.sh                    # Start script (server + CLI modes)
└── main.py                     # CLI entry point
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/strategies` | List all strategies |
| POST | `/api/strategies/activate` | Toggle strategy on/off |
| POST | `/api/strategies/weights` | Update strategy weights |
| GET | `/api/watchlist` | Get watchlist with metadata |
| POST | `/api/watchlist/add` | Add ticker to watchlist |
| GET | `/api/scan` | Run signal generation |
| GET | `/api/portfolio` | Get portfolio state |
| POST | `/api/trade` | Execute paper trade |
| GET | `/api/backtest` | Run backtest |
| GET | `/api/performance` | Get performance metrics |
| GET | `/api/llm/providers` | List LLM providers |
| POST | `/api/llm/analyze` | Run LLM analysis |
| WS | `/ws/signals` | Real-time signal stream |
| WS | `/ws/prices` | Real-time price stream |

## LLM Integration

ProxiAlpha supports multiple LLM providers:

| Provider | Setup | Cost |
|----------|-------|------|
| Ollama (local) | `./install.sh` > install Ollama | Free |
| Anthropic Claude | API key in `config_ai_integration.yaml` | Pay per token |
| OpenAI | API key in config | Pay per token |
| Google Gemini | API key in config | Pay per token |
| Custom endpoint | Any OpenAI-compatible URL | Varies |

### Using Ollama (recommended for DGX / local)

No API key needed. Start with `./start.sh --with-ollama` and select a model in the AI Lab tab.

### Using Cloud APIs

1. Set your API key in `config_ai_integration.yaml`
2. Enable `AI_Claude` in `config_strategies.yaml`
3. Run: `./start.sh --ai-signals`

LLM capabilities:
- Generate buy/sell signals with confidence scores
- Modify strategy parameters at runtime
- Add/remove custom trading rules
- Adjust strategy weights in the consensus engine
- Natural language market analysis

## Watchlist

15 pullback stocks across sectors:

| Sector | Tickers |
|--------|---------|
| Crypto / Fintech | COIN, HOOD, SOFI, IREN |
| Tech / SaaS | ORCL, NOW, MSTR |
| Healthcare | HIMS, NVO, UNH |
| Consumer / Retail | NKE, TGT, EL, LULU |
| Pure Crypto | ETH |

## Strategies

| # | Strategy | Category | Weight | Description |
|---|----------|----------|--------|-------------|
| 1 | DipBuyer | Value | 1.2x | Buy at fixed drawdown levels from ATH |
| 2 | Technical | Technical | 1.0x | RSI + MACD + MA composite scoring |
| 3 | Momentum | Trend | 1.0x | Rate of change + relative strength |
| 4 | MeanReversion | Value | 0.9x | Z-score + Bollinger Band reversion |
| 5 | Breakout | Trend | 1.0x | Volume-confirmed price breakouts |
| 6 | TrendFollowing | Trend | 1.1x | Multi-timeframe MA + ADX |
| 7 | DCA | Accumulation | 0.8x | Dollar-cost averaging into pullbacks |
| 8 | PairsTrading | Arbitrage | 0.7x | Statistical arbitrage (correlated pairs) |
| 9 | EarningsPlay | Event | 0.8x | Pre/post earnings momentum |
| 10 | SectorRotation | Macro | 0.9x | Rotate into strongest sectors |
| 11 | Scalping | Short-Term | 0.6x | Volume spike micro-reversals |
| 12 | SwingTrading | Short-Term | 1.0x | Support/resistance + Fibonacci |
| 13 | OptionsFlow | Flow | 0.8x | Unusual options volume detection |
| 14 | CustomRules | Custom | 0.9x | User-defined JSON rule engine |
| 15 | AI (LLM) | AI | 1.5x | Multi-provider LLM signal generation |
