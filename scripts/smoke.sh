#!/usr/bin/env bash
# ProxiAlpha smoke test runner.
# Chains every step in the testing ladder and stops at the first failure.
# Run from anywhere; resolves its own paths.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"

# Make proxialpha/ importable for the test scripts
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

pass=0
fail=0
failed_steps=()

run_step() {
    local name="$1"; shift
    echo
    echo "=============================================================="
    echo "  $name"
    echo "=============================================================="
    if "$@"; then
        pass=$((pass+1))
    else
        fail=$((fail+1))
        failed_steps+=("$name")
    fi
}

run_step "1/8 Import smoke test"    "$PY" scripts/test_imports.py
run_step "2/8 Risk manager checks"  "$PY" scripts/test_risk.py
run_step "3/8 Broker routing"       "$PY" scripts/test_routing.py
run_step "4/8 Backtest + risk"      "$PY" scripts/test_backtest.py
run_step "5/8 Paper trader + risk"  "$PY" scripts/test_paper.py
run_step "6/8 API endpoints"        "$PY" scripts/test_api.py
run_step "7/8 AI strategy (opt.)"   "$PY" scripts/test_ai.py
run_step "8/8 Ollama round-trip (opt.)" "$PY" scripts/test_ollama.py

echo
echo "=============================================================="
echo "  SMOKE TEST RESULTS"
echo "=============================================================="
echo "  passed: $pass"
echo "  failed: $fail"
if [ $fail -gt 0 ]; then
    echo "  failed steps:"
    for s in "${failed_steps[@]}"; do echo "    - $s"; done
    exit 1
fi
echo "  ALL GREEN"
exit 0
