# Task 07 — Agent Boot Sequence and License Client

**Phase:** 2 (Customer Agent)
**Est. effort:** 6 hours
**Prerequisites:** Tasks 01–06. Task 06 must be runnable against a mock server for local testing.

## Objective

Implement the customer-side boot sequence: read or fetch a license, verify it, run the heartbeat loop, manage mode transitions, and expose a minimal health endpoint. This is the first piece of agent-side code to land.

## Context

- Full spec: `docs/specs/phase2-customer-agent.md` §§ 6, 7, 8, 9, 11.
- Wire protocol: `docs/adr/ADR-003-license-token-and-heartbeat.md`.
- The existing ProxiAlpha engine code in `live_trading/`, `paper_trading/`, `backtesting/` is not touched by this task — we're building the supervisor that will wrap it.

## Exact files to create or modify

1. `proxialpha_agent/__init__.py` — new package at repo root.
2. `proxialpha_agent/__main__.py` — entry point.
3. `proxialpha_agent/supervisor.py` — boot sequence, mode state machine, graceful shutdown.
4. `proxialpha_agent/license.py` — JWT verify, persist, rotate, fingerprint.
5. `proxialpha_agent/heartbeat.py` — heartbeat client with backoff.
6. `proxialpha_agent/modes.py` — `Mode` enum: `BOOTING, RUNNING, OFFLINE_GRACE, DEGRADED, REVOKED, STOPPED`.
7. `proxialpha_agent/health.py` — tiny HTTP server on `localhost:9877` serving `/health` and `/metrics`.
8. `proxialpha_agent/settings.py` — env config via pydantic-settings.
9. `proxialpha_agent/cli.py` — subcommand router.
10. `tests/test_agent_boot.py`, `tests/test_license_client.py`, `tests/test_mode_machine.py` — new test files.
11. `pyproject.toml` — add `proxialpha_agent` as a console script: `proxialpha = proxialpha_agent.cli:main`.

## Acceptance criteria

`proxialpha_agent.license.LicenseClient`:
- `load_from_disk(path="/var/lib/proxialpha/license")` → reads file, verifies signature against embedded public key or JWKS fallback, returns a `License` object or raises `LicenseError`.
- `enroll(install_token)` → POSTs to `/agent/enroll` on the control plane, persists the returned license atomically (write to `.tmp`, rename).
- `persist(license, path)` → atomic write with 0600 perms.
- `verify(token_str) -> License` → checks signature, `exp`, `nbf`, `aud`, clock skew ≤ 5 min, `agent_fingerprint` matches local fingerprint.
- `fingerprint() -> str` → reads `/var/lib/proxialpha/fingerprint`; generates a stable UUID and persists if missing.

`proxialpha_agent.modes.Mode` — plain enum. Transitions are a method on `Supervisor`, not on the enum.

`proxialpha_agent.supervisor.Supervisor`:
- `async def boot()` implements steps 1–13 from Phase 2 PRD §8.
- `async def run()` enters the main loop: keeps heartbeat task alive, watches for SIGTERM, performs graceful shutdown.
- Mode transitions:
  - `BOOTING → RUNNING` when first successful heartbeat lands.
  - `RUNNING → OFFLINE_GRACE` on heartbeat 503 or network error, while `now < grace_until`.
  - `OFFLINE_GRACE → DEGRADED` when `now >= grace_until`.
  - `RUNNING → DEGRADED` on heartbeat 402 (past due).
  - `DEGRADED → RUNNING` on heartbeat 200 with a fresh token.
  - `any → REVOKED` on heartbeat 403; immediately stops engine and exits with code 1 after 5-second grace for log flush.
  - On 409 (fingerprint mismatch): log WARN, stop everything, exit 1.
- Every transition emits a diary event with the mode change reason.

`proxialpha_agent.heartbeat.HeartbeatClient`:
- `async def start(on_response: Callable[[HeartbeatResponse], None])` — runs forever.
- 60-second base interval.
- On startup, first minute: 10-second interval (5 heartbeats to converge quickly).
- On failure: exponential backoff — `min(interval * 2, 300)`. Reset to 60s on success.
- Always sends the metrics snapshot from the Supervisor (paper trades, live trades, signals, backtests, errors in last hour).
- Propagates non-retryable errors (401, 403, 409) to the supervisor immediately; retryable errors (503, 429, network) stay inside the client.

`proxialpha_agent.health` HTTP server:
- `GET /health` → 200 with `{"mode": "RUNNING", "version": "1.0.0", "last_heartbeat_at": "..."}`
- `GET /metrics` → Prometheus text format.
- Binds to `127.0.0.1:9877` — **never** 0.0.0.0. This is a local-only endpoint.

Tests:
- `test_license_load_valid_token` — fixture with a dev keypair, mint a token, load it, assert fields.
- `test_license_load_expired_raises`
- `test_license_load_signature_mismatch_raises`
- `test_license_persist_atomic_and_0600`
- `test_license_fingerprint_stable_across_calls`
- `test_heartbeat_client_60s_cadence` — use time-mocking (freezegun).
- `test_heartbeat_client_backoff_on_503`
- `test_heartbeat_client_resets_to_60s_on_success_after_backoff`
- `test_supervisor_boot_success_reaches_running_mode`
- `test_supervisor_503_enters_offline_grace`
- `test_supervisor_grace_expires_to_degraded`
- `test_supervisor_403_revoked_exits_1`
- `test_supervisor_409_fingerprint_mismatch_exits_1`
- `test_supervisor_402_past_due_blocks_live_keeps_paper`
- `test_health_endpoint_returns_mode`
- `test_health_endpoint_localhost_only` — try binding a client from 0.0.0.0, assert refused.

## Do not

- Do not wire up the real broker adapters yet. The engine stub for this task is a no-op that just increments metric counters on a timer. The real engine integration is Task 08.
- Do not use `requests` or synchronous HTTP inside async code. Use `httpx.AsyncClient`.
- Do not write to `/var/lib/proxialpha` outside the persist + fingerprint paths. Diary writing is Task 08.
- Do not `print` anything. All output goes through `logging` with structured JSON format (configure at program start in `__main__.py`).
- Do not trust `settings.control_plane_url` to have no trailing slash. Normalize it.

## Hints and gotchas

- SIGTERM handling: install a handler in `__main__.py` that sets an asyncio Event; the supervisor's main loop checks the event and initiates graceful shutdown. The entire shutdown should complete within 30 seconds or the container orchestrator will hard-kill.
- The dev public key should live at `proxialpha_agent/keys/dev_pub.pem` and be bundled with the package. Production builds override it via `AGENT_JWKS_URL`.
- Mode transitions must be observable from tests — expose `supervisor.mode` as a read-only property and `supervisor.on_mode_change(callback)` for subscription.
- The fingerprint file must survive container restarts but not container re-creates with a fresh volume. Persisting to `/var/lib/proxialpha/fingerprint` in the mounted volume is correct.
- On first boot (no license yet), look for `PROXIALPHA_INSTALL_TOKEN` in env. If present, call `enroll()`. If absent, log an error with the install instructions from the dashboard and exit 1.

## Test command

```bash
pytest tests/test_agent_boot.py tests/test_license_client.py tests/test_mode_machine.py -v
```

All 16 tests must pass. Also a smoke:

```bash
# Start a mock control plane in another shell:
python -m tests.mock_control_plane

# Then start the agent against it:
PROXIALPHA_CONTROL_PLANE_URL=http://localhost:8001 \
PROXIALPHA_INSTALL_TOKEN=test_install_token \
PROXIALPHA_HOME=/tmp/proxialpha_test \
python -m proxialpha_agent
```

Expected: logs show boot → heartbeat → RUNNING mode. Ctrl-C → graceful shutdown within 5 seconds.
