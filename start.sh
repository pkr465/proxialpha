#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ProxiAlpha — Start Script
# Starts the trading platform with optional Ollama and scan.
#
# Usage:
#   ./start.sh                    # Start API server on port 8000
#   ./start.sh --port 9000        # Custom port
#   ./start.sh --with-ollama      # Start Ollama + API server
#   ./start.sh --scan             # Run signal scan then exit
#   ./start.sh --backtest         # Run backtest then exit
#   ./start.sh --all              # Full analysis (scan + backtest + paper + excel)
#   ./start.sh --install-service  # Install systemd service
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PORT=8000
HOST="0.0.0.0"
WITH_OLLAMA=false
MODE=""
INSTALL_SERVICE=false

# ---- Colors ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---- Parse arguments ----
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --port)         PORT="$2"; shift 2 ;;
            --host)         HOST="$2"; shift 2 ;;
            --with-ollama)  WITH_OLLAMA=true; shift ;;
            --scan)         MODE="scan"; shift ;;
            --backtest)     MODE="backtest"; shift ;;
            --all)          MODE="all"; shift ;;
            --ai-signals)   MODE="ai-signals"; shift ;;
            --paper)        MODE="paper"; shift ;;
            --excel)        MODE="excel"; shift ;;
            --install-service) INSTALL_SERVICE=true; shift ;;
            -h|--help)      print_help; exit 0 ;;
            *)              warn "Unknown option: $1"; shift ;;
        esac
    done
}

print_help() {
    echo ""
    echo "ProxiAlpha — AI Trading Platform"
    echo ""
    echo "Usage: ./start.sh [OPTIONS]"
    echo ""
    echo "Server:"
    echo "  (no args)              Start API server on port 8000"
    echo "  --port PORT            Custom port (default: 8000)"
    echo "  --host HOST            Bind address (default: 0.0.0.0)"
    echo "  --with-ollama          Start Ollama before the API server"
    echo ""
    echo "Run modes (execute and exit):"
    echo "  --scan                 Scan watchlist for signals"
    echo "  --backtest             Run historical backtest"
    echo "  --paper                Paper trading simulator"
    echo "  --ai-signals           Generate AI-powered signals"
    echo "  --excel                Generate Excel tracker"
    echo "  --all                  Run all modes"
    echo ""
    echo "System:"
    echo "  --install-service      Install systemd service (Linux)"
    echo "  -h, --help             Show this help"
    echo ""
}

# ---- Check prerequisites ----
check_prereqs() {
    if [ ! -d "$VENV_DIR" ]; then
        fail "Virtual environment not found. Run ./install.sh first."
    fi
}

# ---- Activate venv ----
activate_venv() {
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
}

# ---- Start Ollama ----
start_ollama() {
    if ! command -v ollama &>/dev/null; then
        warn "Ollama not installed. Run ./install.sh to set it up."
        return
    fi

    # Check if already running
    if curl -s http://localhost:11434/api/tags &>/dev/null; then
        local models
        models=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['name'] for m in d.get('models',[])) or 'none')" 2>/dev/null || echo "unknown")
        ok "Ollama already running — models: $models"
        return
    fi

    info "Starting Ollama..."
    ollama serve &>/dev/null &
    OLLAMA_PID=$!

    # Wait for Ollama to be ready
    local retries=0
    while ! curl -s http://localhost:11434/api/tags &>/dev/null; do
        retries=$((retries + 1))
        if [ "$retries" -gt 30 ]; then
            fail "Ollama failed to start after 30 seconds"
        fi
        sleep 1
    done

    ok "Ollama started (PID $OLLAMA_PID)"

    # Cleanup on exit
    trap "kill $OLLAMA_PID 2>/dev/null; echo ''; info 'Ollama stopped.'" EXIT
}

# ---- Run CLI mode ----
run_mode() {
    info "Running: python main.py --mode $MODE"
    echo ""
    cd "$SCRIPT_DIR"
    python main.py --mode "$MODE"
}

# ---- Install systemd service ----
install_systemd_service() {
    if [[ "$(uname)" != "Linux" ]]; then
        fail "systemd services are only supported on Linux"
    fi

    local user
    user=$(whoami)
    local service_file="/etc/systemd/system/proxialpha.service"

    info "Creating systemd service..."

    sudo tee "$service_file" > /dev/null <<SERVICEEOF
[Unit]
Description=ProxiAlpha Trading Engine
After=network.target ollama.service

[Service]
User=$user
WorkingDirectory=$SCRIPT_DIR
Environment="PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin"
ExecStart=$VENV_DIR/bin/uvicorn api.server:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICEEOF

    sudo systemctl daemon-reload
    sudo systemctl enable proxialpha
    sudo systemctl start proxialpha

    ok "Service installed and started"
    echo ""
    echo "  Status:  sudo systemctl status proxialpha"
    echo "  Logs:    journalctl -u proxialpha -f"
    echo "  Stop:    sudo systemctl stop proxialpha"
    echo "  Restart: sudo systemctl restart proxialpha"
    echo ""
}

# ---- Start API server ----
start_server() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  ProxiAlpha Trading Engine${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo -e "  Dashboard:  ${CYAN}http://localhost:$PORT${NC}"
    echo -e "  API docs:   ${CYAN}http://localhost:$PORT/docs${NC}"
    echo -e "  WebSocket:  ${DIM}ws://localhost:$PORT/ws/signals${NC}"
    echo ""

    if command -v ollama &>/dev/null && curl -s http://localhost:11434/api/tags &>/dev/null; then
        echo -e "  Ollama:     ${GREEN}connected${NC} (localhost:11434)"
    else
        echo -e "  Ollama:     ${DIM}not running${NC}"
    fi

    echo ""
    echo -e "  ${DIM}Press Ctrl+C to stop${NC}"
    echo ""

    cd "$SCRIPT_DIR"
    uvicorn api.server:app --host "$HOST" --port "$PORT"
}

# ============================================================
# MAIN
# ============================================================
main() {
    parse_args "$@"
    check_prereqs
    activate_venv
    cd "$SCRIPT_DIR"

    # Install systemd service
    if [ "$INSTALL_SERVICE" = true ]; then
        install_systemd_service
        exit 0
    fi

    # Start Ollama if requested
    if [ "$WITH_OLLAMA" = true ]; then
        start_ollama
    fi

    # Run specific mode
    if [ -n "$MODE" ]; then
        run_mode
        exit 0
    fi

    # Default: start server
    start_server
}

main "$@"
