#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ProxiAlpha — Installation Script
# Installs Python dependencies, Ollama (optional), and
# configures the platform for first run.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON=""

# ---- Colors ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---- Detect Python ----
detect_python() {
    for cmd in python3.12 python3.11 python3.10 python3; do
        if command -v "$cmd" &>/dev/null; then
            PYTHON="$cmd"
            return
        fi
    done
    fail "Python 3.10+ not found. Install it first:
  macOS:   brew install python3
  Ubuntu:  sudo apt install python3 python3-venv
  RHEL:    sudo dnf install python3"
}

# ---- Check Python version ----
check_python_version() {
    local ver
    ver=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
        fail "Python 3.10+ required (found $ver)"
    fi
    ok "Python $ver ($PYTHON)"
}

# ---- Create virtual environment ----
setup_venv() {
    if [ -d "$VENV_DIR" ]; then
        info "Virtual environment already exists at $VENV_DIR"
    else
        info "Creating virtual environment..."
        $PYTHON -m venv "$VENV_DIR"
        ok "Virtual environment created"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "Activated venv"
}

# ---- Install Python dependencies ----
install_python_deps() {
    info "Installing Python dependencies..."
    pip install --upgrade pip --quiet

    pip install --quiet \
        yfinance \
        pandas \
        numpy \
        openpyxl \
        plotly \
        pyyaml \
        fastapi \
        uvicorn[standard] \
        httpx \
        websockets

    ok "Python dependencies installed"
}

# ---- Generate requirements.txt ----
generate_requirements() {
    info "Generating requirements.txt..."
    pip freeze > "$SCRIPT_DIR/requirements.txt"
    ok "requirements.txt written"
}

# ---- Install Ollama (optional) ----
install_ollama() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  Local LLM Setup (Ollama)${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    if command -v ollama &>/dev/null; then
        ok "Ollama already installed ($(ollama --version 2>/dev/null || echo 'unknown version'))"
    else
        echo -e "Ollama lets you run LLMs locally — no API keys needed."
        echo ""
        read -rp "Install Ollama? [y/N] " choice
        if [[ "$choice" =~ ^[Yy]$ ]]; then
            info "Installing Ollama..."
            curl -fsSL https://ollama.com/install.sh | sh
            ok "Ollama installed"
        else
            info "Skipping Ollama. You can install it later: curl -fsSL https://ollama.com/install.sh | sh"
            return
        fi
    fi

    # Check for NVIDIA GPU
    if command -v nvidia-smi &>/dev/null; then
        echo ""
        local vram
        vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | xargs)
        ok "NVIDIA GPU detected (${vram} MB VRAM)"

        echo ""
        echo "Recommended models for your GPU:"
        if [ -n "$vram" ] && [ "$vram" -ge 40000 ]; then
            echo "  llama3.1:70b       (best quality, ~40GB VRAM)"
            echo "  deepseek-coder     (code analysis)"
            echo "  mixtral:8x22b      (multi-expert, 80GB+)"
        elif [ -n "$vram" ] && [ "$vram" -ge 20000 ]; then
            echo "  llama3.1:8b        (fast, capable)"
            echo "  deepseek-coder     (code analysis)"
            echo "  mistral            (general purpose)"
        else
            echo "  llama3.1:8b        (fast, 8GB VRAM)"
            echo "  mistral            (general purpose)"
        fi
    else
        warn "No NVIDIA GPU detected. Ollama will use CPU (slower)."
        echo "  Recommended: llama3.1:8b, mistral"
    fi

    echo ""
    read -rp "Pull a model now? [y/N] " pull_choice
    if [[ "$pull_choice" =~ ^[Yy]$ ]]; then
        read -rp "Model name [llama3.1:8b]: " model_name
        model_name="${model_name:-llama3.1:8b}"
        info "Pulling $model_name (this may take a few minutes)..."
        ollama pull "$model_name"
        ok "$model_name ready"
    fi
}

# ---- Verify installation ----
verify() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  Verification${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Check Python imports
    "$VENV_DIR/bin/python" -c "
import yfinance, pandas, numpy, yaml, fastapi, uvicorn
print('Python packages: OK')
" && ok "All Python packages importable" || fail "Some packages failed to import"

    # Check config files
    local missing=0
    for cfg in config_strategies.yaml config_watchlist.yaml config_trading.yaml config_ai_integration.yaml config_dashboard.yaml; do
        if [ ! -f "$SCRIPT_DIR/$cfg" ]; then
            warn "Missing config: $cfg"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ] && ok "All config files present"

    # Check Ollama
    if command -v ollama &>/dev/null; then
        if curl -s http://localhost:11434/api/tags &>/dev/null; then
            local models
            models=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['name'] for m in d.get('models',[])) or 'none')" 2>/dev/null || echo "unknown")
            ok "Ollama running — models: $models"
        else
            warn "Ollama installed but not running. Start with: ollama serve"
        fi
    fi
}

# ---- Print summary ----
print_summary() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Installation Complete${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  To start ProxiAlpha:"
    echo ""
    echo "    ./start.sh                  # Start API server"
    echo "    ./start.sh --with-ollama    # Start with local LLM"
    echo "    ./start.sh --scan           # Run a signal scan"
    echo ""
    echo "  Or manually:"
    echo ""
    echo "    source venv/bin/activate"
    echo "    uvicorn api.server:app --host 0.0.0.0 --port 8000"
    echo ""
    echo "  Dashboard: http://localhost:8000"
    echo "  API docs:  http://localhost:8000/docs"
    echo ""
}

# ============================================================
# MAIN
# ============================================================
main() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  ProxiAlpha Installer${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    cd "$SCRIPT_DIR"

    detect_python
    check_python_version
    setup_venv
    install_python_deps
    generate_requirements
    install_ollama
    verify
    print_summary
}

main "$@"
