# ProxiAlpha

ProxiAlpha — AI-powered multi-strategy trading platform. Hosted at proxiant.ai/proxialpha

## Quick Start

```bash
# Install dependencies
pip install yfinance pandas numpy openpyxl plotly pyyaml fastapi uvicorn

# Run full analysis (scan + backtest + paper + excel)
python main.py --mode all

# Individual modes
python main.py --mode scan          # Scan watchlist for signals
python main.py --mode backtest      # Run historical backtest
python main.py --mode paper         # Paper trading simulator
python main.py --mode live          # Live trading via Alpaca
python main.py --mode ai-signals    # Claude AI signal generation
python main.py --mode excel         # Generate Excel tracker

# Start the web dashboard API
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

## Install on DGX Station (GPU + Local LLM)

Run ProxiAlpha on a DGX station with Ollama for fully local, private LLM inference — no API keys, no cloud, no data leaving your machine.

### Prerequisites

- NVIDIA DGX Station (or any Linux machine with NVIDIA GPUs)
- CUDA drivers installed
- Python 3.10+
- Docker (optional, for containerized Ollama)

### Step 1: Install Ollama

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Verify GPU detection
ollama --version
nvidia-smi   # confirm GPUs are visible

# Pull models (choose based on your VRAM)
ollama pull llama3.1:70b      # 40GB VRAM — best quality
ollama pull llama3.1:8b       # 8GB VRAM — fast and capable
ollama pull deepseek-coder    # code-focused analysis
ollama pull mistral           # good general-purpose alternative

# Ollama runs on localhost:11434 by default
# Verify it's running:
curl http://localhost:11434/api/tags
```

### Step 2: Install ProxiAlpha

```bash
# Clone the repo
git clone <your-repo-url> ~/proxialpha
cd ~/proxialpha

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install yfinance pandas numpy openpyxl plotly pyyaml fastapi uvicorn
```

### Step 3: Configure Ollama as the LLM Provider

Edit `config_ai_integration.yaml`:

```yaml
anthropic:
  api_key: null   # not needed for Ollama

# Add Ollama config — no API key required
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
  provider: "ollama"    # use local Ollama instead of cloud API
```

### Step 4: Start the Platform

```bash
# Terminal 1 — Ollama (if not already running as a service)
ollama serve

# Terminal 2 — ProxiAlpha API server
cd ~/proxialpha
source venv/bin/activate
uvicorn api.server:app --host 0.0.0.0 --port 8000

# Terminal 3 — Run a scan with AI signals
python main.py --mode ai-signals
```

The dashboard is live at `http://YOUR_DGX_IP:8000`.
The web terminal at `proxiant.ai/terminal.html` can connect by setting the API URL.

### Step 5: Run as a System Service

Create `/etc/systemd/system/proxialpha.service`:

```ini
[Unit]
Description=ProxiAlpha Trading Engine
After=network.target ollama.service

[Service]
User=your-username
WorkingDirectory=/home/your-username/proxialpha
Environment="PATH=/home/your-username/proxialpha/venv/bin:/usr/bin"
ExecStart=/home/your-username/proxialpha/venv/bin/uvicorn api.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable proxialpha
sudo systemctl start proxialpha

# Check status
sudo systemctl status proxialpha
journalctl -u proxialpha -f   # follow logs
```

### Step 6: Optional — HTTPS with Nginx

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

Then update `terminal.html` to point at your DGX:

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
├── core/                    # Data engine, config, demo data
├── strategies/              # Modular strategy plugins
│   ├── base.py              # BaseStrategy interface
│   ├── dip_buyer.py         # Buy at pullback levels
│   ├── technical.py         # RSI, MACD, MA scoring
│   ├── dca.py               # Dollar-cost averaging
│   ├── custom_rules.py      # JSON rule engine
│   ├── ai_strategy.py       # LLM-powered signal plugin
│   └── strategy_manager.py  # Orchestrator + consensus
├── backtesting/             # Historical backtesting engine
├── paper_trading/           # Paper trading simulator
├── live_trading/            # Alpaca API live trading
├── api/                     # FastAPI REST + WebSocket server
├── web/                     # React dashboard (index.html, app.jsx)
├── config_strategies.yaml   # Strategy parameters
├── config_watchlist.yaml    # Stock universe + thesis
├── config_trading.yaml      # Execution + risk rules
├── config_ai_integration.yaml  # LLM provider settings
├── config_dashboard.yaml    # Dashboard layout config
└── main.py                  # Main entry point
```

## LLM Integration

ProxiAlpha supports multiple LLM providers:

| Provider | Setup | Cost |
|----------|-------|------|
| Ollama (local) | `ollama serve` on localhost:11434 | Free |
| Anthropic Claude | API key in config | Pay per token |
| OpenAI | API key in config | Pay per token |
| Google Gemini | API key in config | Pay per token |
| Custom endpoint | Any OpenAI-compatible URL | Varies |

### Using Ollama (recommended for DGX)

No API key needed. Just run `ollama serve` and select a model in the dashboard's AI Lab tab.

### Using Cloud APIs

1. Set your API key in `config_ai_integration.yaml`
2. Enable `AI_Claude` in `config_strategies.yaml`
3. Run: `python main.py --mode ai-signals`

LLM capabilities:
- Generate buy/sell signals with confidence scores
- Modify strategy parameters at runtime
- Add/remove custom trading rules
- Adjust strategy weights in the consensus engine
- Natural language market analysis

## Watchlist (15 Pullback Stocks)

COIN, HOOD, ORCL, MSTR, ETH, NOW, SOFI, HIMS, NKE, NVO, UNH, IREN, TGT, EL, LULU

## Strategies

| Strategy | Description | Weight |
|----------|-------------|--------|
| DipBuyer | Buy at fixed drawdown levels | 1.2x |
| Technical | RSI + MACD + MA scoring | 1.0x |
| Momentum | ROC + relative strength | 1.0x |
| MeanReversion | Z-score + Bollinger Bands | 0.9x |
| DCA | Gradual accumulation | 0.8x |
| CustomRules | JSON rule engine | 0.9x |
| AI (LLM) | Multi-provider LLM signals | 1.5x |
