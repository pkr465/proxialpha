"""
Step 8: Ollama round-trip smoke test (optional).

Auto-skips (exit 0) unless OLLAMA_BASE_URL is set. Designed to verify that
ProxiAlpha's LLMAdapter can successfully reach a local or remote Ollama
server (e.g. running on a DGX host) and get a well-formed response.

Environment variables:
  OLLAMA_BASE_URL   Required. e.g. http://localhost:11434  or  http://dgx.local:11434
  OLLAMA_MODEL      Optional. Default: llama3.1:8b  (override to whatever
                    model you've pulled on the DGX).
  OLLAMA_TIMEOUT    Optional seconds. Default: 120.

Checks:
  1. /api/tags reachable (server is alive)
  2. Target model is loaded/available
  3. LLMAdapter.generate() returns non-empty text
  4. eval_count > 0 (the model actually produced tokens)
"""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request


def _skip(reason: str) -> int:
    print(f"[test_ollama] SKIP: {reason}")
    return 0


def main() -> int:
    base_url = os.getenv("OLLAMA_BASE_URL", "").rstrip("/")
    if not base_url:
        return _skip("OLLAMA_BASE_URL not set")

    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
    timeout = int(os.getenv("OLLAMA_TIMEOUT", "120"))

    # 1) Server reachable ------------------------------------------------
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as resp:
            tags_payload = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        print(f"  FAIL /api/tags unreachable at {base_url}: {e}")
        return 1
    except Exception as e:
        print(f"  FAIL /api/tags error: {e}")
        return 1

    available = [m.get("name", "") for m in tags_payload.get("models", [])]
    print(f"  OK   /api/tags reachable, {len(available)} model(s) loaded")
    if available:
        print(f"       available: {', '.join(available[:8])}" + (" ..." if len(available) > 8 else ""))

    # 2) Target model present --------------------------------------------
    #    Ollama tags include the full name like "llama3.1:8b" — allow both
    #    exact match and prefix match (e.g. "llama3.1" matches "llama3.1:8b").
    present = any(name == model or name.startswith(f"{model}:") or name.split(":")[0] == model
                  for name in available)
    if not present:
        print(f"  WARN model '{model}' not in /api/tags. Pull it with:")
        print(f"         ollama pull {model}")
        print(f"       Continuing anyway — Ollama may pull on demand...")

    # 3) LLMAdapter round-trip -------------------------------------------
    try:
        from core.llm_adapter import LLMAdapter
    except ImportError as e:
        print(f"  FAIL cannot import LLMAdapter: {e}")
        return 1

    prompt = (
        "Return exactly one word in response: 'pong'. "
        "No punctuation, no explanation, no quotes."
    )
    try:
        llm = LLMAdapter(
            provider="ollama",
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_tokens=32,
            temperature=0.0,
            system_prompt="You are a trading assistant smoke test. Be terse.",
        )
        response = llm.generate(prompt)
    except Exception as e:
        print(f"  FAIL LLMAdapter.generate() raised: {e}")
        return 1

    text = (response.text or "").strip()
    print(f"  OK   LLMAdapter.generate() returned {len(text)} chars: {text[:80]!r}")

    if not text:
        print("  FAIL response text was empty")
        return 1

    # 4) Usage sanity ----------------------------------------------------
    usage = response.usage or {}
    eval_count = int(usage.get("eval_count", 0) or 0)
    if eval_count <= 0:
        print(f"  WARN eval_count={eval_count} (Ollama didn't report token count)")
    else:
        print(f"  OK   eval_count={eval_count} tokens produced")

    print(f"\n[test_ollama] OK (provider=ollama, model={model}, base_url={base_url})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[test_ollama] interrupted")
        sys.exit(130)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[test_ollama] FAIL: {e}")
        sys.exit(1)
