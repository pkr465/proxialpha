#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ProxiAlpha — Installation Script
# Installs Python dependencies via uv (preferred) or pip,
# Ollama (optional), and configures the platform for first run.
#
# Usage:
#   ./install.sh                # Auto-detect uv or pip
#   ./install.sh --with-claude  # Include Anthropic SDK
#   ./install.sh --with-openai  # Include OpenAI SDK
#   ./install.sh --with-all     # All optional LLM + trading deps
#   ./install.sh --pip          # Force pip (skip uv)
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON=""
USE_UV=false
EXTRAS=""
FORCE_PIP=false

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
            --with-claude)  EXTRAS="claude"; shift ;;
            --with-openai)  EXTRAS="openai"; shift ;;
            --with-gemini)  EXTRAS="gemini"; shift ;;
            --with-all-llm) EXTRAS="all-llm"; shift ;;
            --with-alpaca)  EXTRAS="alpaca"; shift ;;
            --with-all)     EXTRAS="all"; shift ;;
            --pip)          FORCE_PIP=true; shift ;;
            -h|--help)      print_help; exit 0 ;;
            *)              warn "Unknown option: $1"; shift ;;
        esac
    done
}

print_help() {
    echo ""
    echo "ProxiAlpha Installer"
    echo ""
    echo "Usage: ./install.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --with-claude     Include Anthropic Claude SDK"
    echo "  --with-openai     Include OpenAI SDK"
    echo "  --with-gemini     Include Google Gemini SDK"
    echo "  --with-all-llm    Include all LLM provider SDKs"
    echo "  --with-alpaca     Include Alpaca live trading SDK"
    echo "  --with-all        Include all optional dependencies"
    echo "  --pip             Force pip install (skip uv even if available)"
    echo "  -h, --help        Show this help"
    echo ""
}

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

# ---- Detect or install uv ----
setup_uv() {
    if [ "$FORCE_PIP" = true ]; then
        info "Skipping uv (--pip flag set)"
        return
    fi

    if command -v uv &>/dev/null; then
        USE_UV=true
        ok "uv $(uv --version 2>/dev/null | head -1)"
        return
    fi

    echo ""
    echo -e "  ${CYAN}uv${NC} is a fast Python package manager (10-100x faster than pip)."
    echo ""
    read -rp "Install uv? [Y/n] " choice
    if [[ ! "$choice" =~ ^[Nn]$ ]]; then
        info "Installing uv..."
        curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null

        # Add to PATH for this session
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

        if command -v uv &>/dev/null; then
            USE_UV=true
            ok "uv installed ($(uv --version 2>/dev/null | head -1))"
        else
            warn "uv install completed but not found in PATH. Falling back to pip."
        fi
    else
        info "Using pip instead"
    fi
}

# ---- Create virtual environment ----
setup_venv() {
    if [ -d "$VENV_DIR" ]; then
        info "Virtual environment already exists at $VENV_DIR"
    else
        info "Creating virtual environment..."
        if [ "$USE_UV" = true ]; then
            uv venv "$VENV_DIR" --python "$PYTHON"
        else
            $PYTHON -m venv "$VENV_DIR"
        fi
        ok "Virtual environment created"
    fi

    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "Activated venv"
}

# ---- Install Python dependencies ----
install_deps() {
    info "Installing dependencies from requirements.txt..."

    if [ "$USE_UV" = true ]; then
        # uv install from requirements.txt
        uv pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

        # Install optional extras if requested
        if [ -n "$EXTRAS" ]; then
            info "Installing optional extras: $EXTRAS"
            uv pip install -e ".[$EXTRAS]" --quiet
        fi

        ok "Dependencies installed via uv"
    else
        pip install --upgrade pip --quiet
        pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

        # Install optional extras if requested
        if [ -n "$EXTRAS" ]; then
            info "Installing optional extras: $EXTRAS"
            pip install -e ".[$EXTRAS]" --quiet
        fi

        ok "Dependencies installed via pip"
    fi
}

# ---- Lock dependencies ----
lock_deps() {
    if [ "$USE_UV" = true ]; then
        info "Locking dependencies..."
        uv pip freeze > "$SCRIPT_DIR/requirements.lock" 2>/dev/null
        ok "requirements.lock written (uv)"
    else
        info "Freezing dependencies..."
        pip freeze > "$SCRIPT_DIR/requirements.lock" 2>/dev/null
        ok "requirements.lock written (pip)"
    fi
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
            info "Skipping Ollama. Install later: curl -fsSL https://ollama.com/install.sh | sh"
            return
        fi
    fi

    # Check for NVIDIA GPU
    if command -v nvidia-smi &>/dev/null; then
        echo ""
        local vram
        vram=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d '[:space:]')
        if [ -n "$vram" ] && [ "$vram" -gt 0 ] 2>/dev/null; then
            ok "NVIDIA GPU detected (${vram} MB VRAM)"
            echo ""
            echo "Recommended models:"
            if [ "$vram" -ge 40000 ]; then
                echo "  llama3.1:70b       (best quality, ~40GB VRAM)"
                echo "  deepseek-coder     (code analysis)"
                echo "  mixtral:8x22b      (multi-expert, 80GB+)"
            elif [ "$vram" -ge 20000 ]; then
                echo "  llama3.1:8b        (fast, capable)"
                echo "  deepseek-coder     (code analysis)"
                echo "  mistral            (general purpose)"
            else
                echo "  llama3.1:8b        (fast, 8GB VRAM)"
                echo "  mistral            (general purpose)"
            fi
        else
            warn "nvidia-smi found but could not read VRAM"
        fi
    elif [[ "$(uname)" == "Darwin" ]]; then
        # macOS — Apple Silicon runs Ollama on Metal
        if [[ "$(uname -m)" == "arm64" ]]; then
            ok "Apple Silicon detected (Ollama uses Metal GPU)"
            echo ""
            echo "Recommended models:"
            echo "  llama3.1:8b        (fast, capable)"
            echo "  mistral            (general purpose)"
        else
            info "Intel Mac — Ollama will use CPU"
        fi
    else
        warn "No GPU detected. Ollama will use CPU (slower)."
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

    # Check core imports
    "$VENV_DIR/bin/python" -c "
import yfinance, pandas, numpy, yaml, fastapi, uvicorn, pydantic, httpx, openpyxl, plotly
print('All core packages OK')
" && ok "Core packages importable" || fail "Some core packages failed to import"

    # Check optional imports
    local optional_status=""
    for pkg in anthropic openai; do
        if "$VENV_DIR/bin/python" -c "import $pkg" 2>/dev/null; then
            optional_status="$optional_status $pkg"
        fi
    done
    if [ -n "$optional_status" ]; then
        ok "Optional packages:$optional_status"
    else
        info "Optional LLM SDKs not installed (use --with-claude, --with-openai, etc.)"
    fi

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
        if curl -s --max-time 3 http://localhost:11434/api/tags &>/dev/null; then
            local models
            models=$(curl -s --max-time 3 http://localhost:11434/api/tags 2>/dev/null \
                | "$VENV_DIR/bin/python" -c "import sys,json; d=json.load(sys.stdin); print(', '.join(m['name'] for m in d.get('models',[])) or 'none')" 2>/dev/null \
                || echo "unknown")
            ok "Ollama running — models: $models"
        else
            warn "Ollama installed but not running. Start with: ollama serve"
        fi
    fi

    # Show installer info
    echo ""
    if [ "$USE_UV" = true ]; then
        ok "Package manager: uv ($(uv --version 2>/dev/null | head -1))"
    else
        ok "Package manager: pip ($(pip --version 2>/dev/null | awk '{print $2}'))"
    fi
}

# ---- Print summary ----
print_summary() {
    echo ""
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  Installation Complete${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  Start ProxiAlpha:"
    echo ""
    echo "    ./start.sh                  # API server on :8000"
    echo "    ./start.sh --with-ollama    # Start with local LLM"
    echo "    ./start.sh --scan           # Run a signal scan"
    echo ""
    echo "  Optional extras (re-run install with):"
    echo ""
    echo "    ./install.sh --with-claude  # Anthropic Claude SDK"
    echo "    ./install.sh --with-openai  # OpenAI SDK"
    echo "    ./install.sh --with-all     # All LLM + trading SDKs"
    echo ""
    echo -e "  Dashboard:  ${CYAN}http://localhost:8000${NC}"
    echo -e "  API docs:   ${CYAN}http://localhost:8000/docs${NC}"
    echo ""
}

# ============================================================
# MAIN
# ============================================================
main() {
    parse_args "$@"

    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  ProxiAlpha Installer${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    cd "$SCRIPT_DIR"

    detect_python
    check_python_version
    setup_uv
    setup_venv
    install_deps
    lock_deps
    install_ollama
    verify
    print_summary
}

main "$@"
