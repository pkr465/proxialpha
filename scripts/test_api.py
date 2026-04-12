"""
Step 6: FastAPI server smoke test.
Uses Starlette's TestClient (in-process, no uvicorn, no open port) to hit
the three new endpoints added in the Hyperliquid integration:
  /api/diary
  /api/llm-logs
  /api/risk/summary
"""
from __future__ import annotations
import sys

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("[test_api] fastapi[testclient] not installed — skipping")
    sys.exit(0)

from api.server import app


def main() -> int:
    client = TestClient(app)

    # /api/risk/summary should always work (no state required)
    r = client.get("/api/risk/summary")
    assert r.status_code == 200, f"risk/summary: {r.status_code} {r.text}"
    data = r.json()
    assert "risk" in data, f"missing 'risk' key: {data}"
    assert "max_position_pct" in data["risk"], f"unexpected risk summary shape: {data['risk']}"
    print(f"  OK   /api/risk/summary -> max_position_pct={data['risk']['max_position_pct']}")

    # /api/llm-logs should return a dict with 'log' and 'path'
    r = client.get("/api/llm-logs?n_bytes=1024")
    assert r.status_code == 200, f"llm-logs: {r.status_code} {r.text}"
    data = r.json()
    assert "log" in data and "path" in data, f"unexpected llm-logs shape: {data}"
    print(f"  OK   /api/llm-logs -> path={data['path']}, bytes={len(data['log'])}")

    # /api/diary with each source flavor
    for source in ("paper", "live", "backtest", "ai"):
        r = client.get(f"/api/diary?source={source}&limit=5")
        assert r.status_code == 200, f"diary {source}: {r.status_code} {r.text}"
        data = r.json()
        # Expect a list (could be empty)
        entries = data if isinstance(data, list) else data.get("entries", data)
        print(f"  OK   /api/diary?source={source} -> {type(entries).__name__} ({len(entries) if hasattr(entries,'__len__') else '?'} entries)")

    print("\n[test_api] OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"[test_api] FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[test_api] FAIL: {e}")
        sys.exit(1)
