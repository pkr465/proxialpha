# Phase 2 PRD — Customer Agent

**Status:** Draft, ready for implementation
**Phase:** 2 of 6
**Owner:** Pavan
**Target duration:** Weeks 5–8 (overlaps Phase 1 by 1 week)
**Depends on:** Phase 1 (entitlements + license issuance), ADR-001, ADR-003, ADR-004
**Blocks:** Phase 3 (LLM gateway testing), Phase 4 (dashboard needs real agent data)

---

## 1. Problem Statement

ADR-001 commits us to shipping the trading execution layer as a standalone binary that customers run on their own infrastructure. This phase turns the existing `live_trading/`, `paper_trading/`, `backtesting/`, and broker adapter code into a production-grade Docker image that:

- Boots from a license token and nothing else.
- Authenticates to the control plane, fetches its config bundle, and runs.
- Keeps running through control plane outages (up to 7 days per ADR-003).
- Replicates its diary back to the control plane for dashboard visibility.
- Enforces entitlements locally on the hot path — zero network calls per decision.
- Ships as `proxialpha/agent:1.0.0` with a `proxialpha-cli` wrapper for install and operation.

The measurable goal: a new customer goes from "I have a license token" to "my paper trading loop is running" in under 10 minutes on a Linux box with Docker installed.

## 2. Goals

1. A single Docker image supports Topologies A, B, and C (ADR-001) with runtime selection from the license token.
2. The agent's hot path (order decision → risk gate → broker call) makes zero network calls to the control plane.
3. Agent operates fully for 7 days of no heartbeat contact, then degrades gracefully: blocks live trades, keeps paper + backtests.
4. Diary replication to the control plane is at-most-once-per-record, exactly-once in practice, with idempotent replay on restart.
5. Agent upgrade (`docker pull && docker restart`) preserves local diary and license state.
6. `proxialpha doctor` produces a redacted diagnostic bundle for support.

## 3. Non-Goals

- No orchestration for multiple agents. One customer = one agent in v1.
- No auto-update. Customers pull new images manually.
- No in-agent web UI. All UI is the hosted dashboard.
- No local dashboard API. The agent exposes a minimal `/health` and `/metrics` on localhost only.
- No Windows support. Linux + macOS via Docker only.
- No air-gapped install for v1. Agent requires at least initial contact with the control plane.

## 4. User Stories

### US-2.1: First run on a Linux box
**As** a Pro-tier customer who just paid
**I want** to copy a single `docker run` command from the dashboard and have my agent running in 5 minutes
**So that** I can start paper trading immediately

**Acceptance criteria:**
- Dashboard shows a "Deploy Agent" page with OS-detected instructions and a one-time install token.
- The command is `docker run -d --name proxialpha --restart unless-stopped -v ~/.proxialpha:/var/lib/proxialpha -e PROXIALPHA_INSTALL_TOKEN=<token> proxialpha/agent:1.0.0`.
- On first boot, the agent exchanges the install token for a license token and caches it.
- Within 60 seconds, the dashboard shows `Agent: connected, version 1.0.0, topology C`.
- The user sees paper trading start automatically with the default config bundle.

### US-2.2: Control plane goes offline for 3 hours
**As** a Team-tier customer running a live strategy
**I want** my agent to keep trading through our routine 3-hour control plane outage
**So that** my strategy does not miss opportunities

**Acceptance criteria:**
- Agent sees `503` on heartbeat, enters `offline_grace` mode.
- Live trading, paper trading, backtests all continue.
- Diary buffers locally in `~/.proxialpha/diary-pending.jsonl`.
- When the control plane comes back, buffered diary entries are replayed in order; duplicates are rejected server-side via idempotency keys.
- Dashboard retroactively shows the trades that occurred during the outage.

### US-2.3: Subscription cancelled
**As** the system
**I want** a cancelled customer's agent to stop trading within 24 hours
**So that** we don't continue providing a paid service to non-paying users

**Acceptance criteria:**
- Subscription deleted in Stripe → control plane marks org as Free tier.
- Next heartbeat returns `403 revoked` with a cancellation reason.
- Agent immediately stops placing live orders.
- Agent continues running paper trading for the Free tier's allowed features (reduced).
- Dashboard shows the downgrade reason and a reactivate CTA.

### US-2.4: Team-tier customer runs on a DGX
**As** a quant team with a DGX box running Ollama
**I want** my agent to talk to localhost:11434 for inference instead of the control plane gateway
**So that** no trade data or LLM prompts leave our network

**Acceptance criteria:**
- License token has `topology = C` and `llm_routing = local`.
- Agent reads `OLLAMA_BASE_URL` or the license bundle's `local_llm.base_url` and routes all LLM calls to it.
- Zero control-plane traffic during trading (only heartbeats at 60s cadence).
- Dashboard shows aggregated metrics (without the prompt/response bodies) based on what the heartbeat reports.

### US-2.5: Operator runs `proxialpha doctor` after a crash
**As** the customer's ops person
**I want** to run `docker exec proxialpha proxialpha doctor > bundle.tar.gz` and email it to support
**So that** support can diagnose without needing SSH access

**Acceptance criteria:**
- Command exits 0 with a bundle at stdout or the specified path.
- Bundle contains: agent version, config bundle (redacted), last 5,000 log lines, diary summary (counts only), heartbeat history, last 10 errors, system info (CPU, memory, OS).
- Bundle contains **no** broker API keys, LLM API keys, or private wallet keys. A unit test verifies this.
- Bundle is gzipped, deterministic, and under 5 MB.

## 5. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│  proxialpha/agent:1.0.0  (Docker image, ~400MB)                │
│                                                                │
│  ┌────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Supervisor │─▶│ Config       │─▶│ Engine                 │ │
│  │            │  │ bundle       │  │  ├─ live_trading/      │ │
│  │ - License  │  │ (from CP)    │  │  ├─ paper_trading/     │ │
│  │ - Heartbeat│  │              │  │  ├─ backtesting/       │ │
│  │ - Health   │  │              │  │  └─ risk_manager       │ │
│  └────────────┘  └──────────────┘  └────────────────────────┘ │
│        │                                        │              │
│        │                                        ▼              │
│        │                                  ┌──────────────┐    │
│        │                                  │ Broker       │    │
│        │                                  │ adapters     │    │
│        │                                  │ - Alpaca     │    │
│        │                                  │ - Hyperliquid│    │
│        │                                  └──────────────┘    │
│        │                                        │              │
│        ▼                                        ▼              │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ Diary (JSONL, local-first, replicated in background)     │ │
│  │ - diary.jsonl (committed)                                │ │
│  │ - diary-pending.jsonl (to send)                          │ │
│  └──────────────────────────────────────────────────────────┘ │
│        │                                                       │
│        ▼                                                       │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ LLM adapter (ADR-004)                                    │ │
│  │ - local → Ollama / vLLM on localhost                     │ │
│  │ - byo_key → provider directly                            │ │
│  │ - gateway → control plane /llm/generate                  │ │
│  └──────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
         │                                       ▲
         │   HTTPS heartbeat (60s)                │  License refresh
         ▼                                       │
┌────────────────────────────────────────────────┴────────────────┐
│                    CONTROL PLANE                                │
│                    /agent/heartbeat, /agent/diary, /llm/generate│
└─────────────────────────────────────────────────────────────────┘
```

## 6. Code Structure

```
proxialpha-agent/
├── pyproject.toml
├── Dockerfile
├── proxialpha_agent/
│   ├── __init__.py
│   ├── __main__.py          # entry point: `python -m proxialpha_agent`
│   ├── cli.py               # `proxialpha-cli` (login, start, status, doctor)
│   ├── supervisor.py        # process lifecycle, heartbeat loop, graceful shutdown
│   ├── license.py           # JWT verify, token rotation, fingerprint
│   ├── heartbeat.py         # heartbeat client, backoff, mode transitions
│   ├── config_bundle.py     # fetch + validate + apply config from control plane
│   ├── engine.py            # wraps live_trading / paper_trading / backtesting
│   ├── diary.py             # append-local + background replication
│   ├── llm_router.py        # chooses gateway / byo_key / local path
│   ├── entitlements.py      # local enforcement using license token snapshot
│   ├── doctor.py            # diagnostic bundle generator
│   └── modes.py             # RUNNING, DEGRADED, OFFLINE_GRACE, REVOKED, STOPPED
└── tests/
    ├── test_license.py
    ├── test_heartbeat.py
    ├── test_mode_transitions.py
    ├── test_diary_replication.py
    └── test_doctor_redaction.py
```

The existing repo code (`live_trading/`, `paper_trading/`, `backtesting/`, `core/`) is imported into the agent as a versioned internal package. Keep them in the same repo; build the agent image from a subset.

## 7. Agent Lifecycle & Modes

### Mode state machine

```
          first_boot
             │
             ▼
       ┌───────────┐
   ┌──▶│ BOOTING   │
   │   └───────────┘
   │         │ license token valid, config fetched
   │         ▼
   │   ┌───────────┐
   │   │ RUNNING   │──────────────────┐
   │   └───────────┘                  │
   │         │                        │
   │         │  heartbeat 503         │  heartbeat 402
   │         ▼                        ▼
   │   ┌──────────────┐          ┌───────────┐
   │   │OFFLINE_GRACE │          │ DEGRADED  │
   │   └──────────────┘          └───────────┘
   │         │                        │
   │         │ 7 days no contact      │ subscription paid
   │         ▼                        │
   │   ┌───────────┐                  │
   │   │ DEGRADED  │◀─────────────────┘
   │   └───────────┘
   │         │  heartbeat 403 revoked
   │         ▼
   │   ┌───────────┐
   └───│ REVOKED   │──▶ exit 1
       └───────────┘
```

### Mode capabilities

| Mode | Paper | Backtests | Live equity | Live perps | Heartbeat |
|---|---|---|---|---|---|
| BOOTING | ✗ | ✗ | ✗ | ✗ | trying |
| RUNNING | ✓ | ✓ | ✓ (if tier) | ✓ (if tier) | 60s |
| OFFLINE_GRACE | ✓ | ✓ | ✓ | ✓ | backoff |
| DEGRADED | ✓ | ✓ | ✗ | ✗ | hourly |
| REVOKED | ✗ | ✗ | ✗ | ✗ | — (exits) |

The distinction between `OFFLINE_GRACE` (everything works) and `DEGRADED` (live blocked) is load-bearing: see ADR-003 section on grace asymmetry.

## 8. Boot Sequence

```
1. Read /var/lib/proxialpha/fingerprint or generate + persist.
2. Read /var/lib/proxialpha/license or, if missing, exchange PROXIALPHA_INSTALL_TOKEN
   for a license via POST /agent/enroll.
3. Verify JWT signature against embedded public key (fallback: JWKS).
4. Verify `agent_fingerprint` claim matches local fingerprint.
5. Verify `grace_until > now`.
6. Load entitlements snapshot from license into local enforcer.
7. POST /agent/heartbeat once with version, hostname, topology. Expect fresh token + config bundle.
8. Write fresh token atomically to /var/lib/proxialpha/license.tmp then rename.
9. Apply config bundle: strategies, broker credentials (from env/secrets), risk params, LLM routing.
10. Start engine in mode RUNNING.
11. Start heartbeat loop as a daemon task.
12. Start diary replication loop as a daemon task.
13. Expose /health on localhost:9877 for docker healthcheck.
```

## 9. Heartbeat Loop

- Base cadence 60s; on startup, 10s for the first minute; on failure, exponential backoff capped at 5 minutes.
- Each heartbeat returns a potentially-new license token. Only rotate if `jti` differs.
- Each heartbeat may include a config bundle delta (new strategy, updated risk params). Apply delta to a staging area, swap atomically, log the change to the local diary.
- Transitions between modes are logged as events with the full state diff.

## 10. Diary Replication

The existing JSONL diary (`data/*.jsonl` in the repo) is already append-only and idempotent-friendly. Replicating it:

1. Engine writes new records to `~/.proxialpha/diary.jsonl` with a monotonically-increasing `seq` and a deterministic `idempotency_key` (sha256 of record contents + seq).
2. A background task reads unreplicated records (tracked in `~/.proxialpha/diary.cursor`), batches up to 100 at a time, and POSTs to `/agent/diary`.
3. Control plane upserts on `idempotency_key`. Duplicate replays are silent.
4. On success, advance `diary.cursor`.
5. On network failure, retry indefinitely with exponential backoff. Never drop records.
6. If `~/.proxialpha/diary.jsonl` exceeds 500MB, rotate to `diary.jsonl.N` and start fresh. Replicator handles rotated files seamlessly.

## 11. Entitlement Enforcement (local)

Per ADR-003, the license token carries an `entitlements_snapshot`. The local enforcer:

```python
class LocalEntitlements:
    def __init__(self, snapshot: dict):
        self.tickers_max = snapshot["tickers_max"]
        self.signals_remaining = snapshot["signals_remaining"]
        self.backtests_remaining = snapshot["backtests_remaining"]
        self.live_trading_enabled = snapshot["live_trading_enabled"]
        self.live_perps_enabled = snapshot["live_perps_enabled"]
        self.custom_strategies_enabled = snapshot["custom_strategies_enabled"]
        self.overage_enabled = snapshot["overage_enabled"]
        self._lock = threading.Lock()

    def try_consume_signal(self) -> bool:
        with self._lock:
            if self.signals_remaining > 0:
                self.signals_remaining -= 1
                return True
            return self.overage_enabled

    def can_place_live_order(self, is_perp: bool) -> bool:
        if not self.live_trading_enabled:
            return False
        if is_perp and not self.live_perps_enabled:
            return False
        return True
```

Consumption deltas are sent to the control plane via heartbeat so Stripe metering stays accurate. Stale snapshots (after a heartbeat refresh) replace the enforcer's state atomically.

## 12. LLM Routing

```python
class LLMRouter:
    def __init__(self, license_snapshot, config_bundle):
        self.mode = license_snapshot.get("llm_routing", "gateway")
        if self.mode == "gateway":
            self.adapter = ControlPlaneLLMAdapter(
                base_url=settings.control_plane_url,
                license_provider=heartbeat.current_token,
            )
        elif self.mode == "byo_key":
            self.adapter = LLMAdapter(
                provider=config_bundle["llm"]["provider"],
                api_key=config_bundle["llm"]["api_key"],
                model=config_bundle["llm"]["model"],
            )
        elif self.mode == "local":
            self.adapter = LLMAdapter(
                provider="ollama",
                base_url=config_bundle["llm"]["base_url"],
                model=config_bundle["llm"]["model"],
            )
```

All three conform to the existing `LLMAdapter` interface (`.generate()`), so `AIDecisionMaker` needs zero changes.

## 13. Docker Image Spec

```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv venv && uv sync --frozen

FROM python:3.11-slim
RUN useradd --create-home --uid 1000 proxialpha && \
    mkdir -p /var/lib/proxialpha && \
    chown -R proxialpha:proxialpha /var/lib/proxialpha
COPY --from=builder /build/.venv /app/.venv
COPY proxialpha_agent/ /app/proxialpha_agent/
COPY live_trading/ paper_trading/ backtesting/ core/ strategies/ /app/
ENV PATH=/app/.venv/bin:$PATH \
    PROXIALPHA_HOME=/var/lib/proxialpha
USER proxialpha
VOLUME ["/var/lib/proxialpha"]
EXPOSE 9877
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:9877/health || exit 1
ENTRYPOINT ["python", "-m", "proxialpha_agent"]
```

- Multi-arch build: `linux/amd64`, `linux/arm64`.
- Published to `docker.io/proxialpha/agent` with semver tags and `latest`.
- Image signed with `cosign`; customers can verify via `cosign verify`.
- Target image size: <500 MB compressed.

## 14. CLI

```
proxialpha login <install_token>          # exchange for license, cache
proxialpha start                          # alias for `python -m proxialpha_agent`
proxialpha status                         # print mode, last heartbeat, entitlements
proxialpha diary tail -n 50               # print last N diary entries
proxialpha diary replicate --force        # force replay of unreplicated diary
proxialpha doctor [--output bundle.tar.gz] # diagnostic bundle
proxialpha version                        # print image version + build info
```

All commands are subcommands of `proxialpha_agent.cli:main`, wired up in `pyproject.toml` as a console entry point. Inside the Docker container: `docker exec proxialpha proxialpha <cmd>`.

## 15. Configuration Bundle Format

Sent from control plane on heartbeat when new:

```yaml
version: 2
issued_at: "2026-04-11T09:15:00Z"
strategies:
  - name: trend_following
    enabled: true
    params: {...}
  - name: mean_reversion
    enabled: false
    params: {...}
risk_manager:
  max_position_pct: 10
  min_order_usd: 100
  max_concurrent_positions: 10
  daily_drawdown_pct: 5
  balance_reserve_pct: 10
  max_total_exposure_pct: 200
  max_leverage: 5
brokers:
  alpaca:
    enabled: true
    api_key_env: ALPACA_API_KEY
    api_secret_env: ALPACA_API_SECRET
    paper: false
  hyperliquid:
    enabled: true
    wallet_env: HYPERLIQUID_WALLET_KEY
llm:
  provider: ollama
  base_url: http://localhost:11434
  model: llama3.1:70b
watchlist:
  - AAPL
  - MSFT
  - BTC
  - ETH
```

**Critical:** broker credentials come from environment variables or files mounted into the container. The config bundle only references them by name. Keys are never transmitted from the control plane to the agent.

## 16. Test Plan

### Unit tests

- `test_license_signature_verification`
- `test_license_fingerprint_mismatch_rejects`
- `test_license_expired_rejects`
- `test_license_grace_window_respected`
- `test_mode_transition_running_to_offline_grace_on_503`
- `test_mode_transition_offline_grace_to_degraded_after_7_days`
- `test_mode_transition_degraded_to_revoked_on_403`
- `test_diary_replication_idempotent_on_replay`
- `test_diary_replication_survives_rotation`
- `test_doctor_bundle_contains_no_secrets`
- `test_llm_router_selects_correct_adapter_per_topology`
- `test_entitlement_consume_atomic_under_concurrency`

### Integration tests (against a mock control plane)

- `test_full_boot_sequence_end_to_end`
- `test_license_refresh_rotates_atomically`
- `test_config_bundle_delta_applied_without_restart`
- `test_heartbeat_backoff_on_persistent_failure`
- `test_paper_trading_continues_during_offline_grace`
- `test_live_trading_blocked_in_degraded`
- `test_agent_exits_cleanly_on_revoked`

### End-to-end smoke

- `scripts/test_agent.sh` — builds the image, runs it with a test license, waits for health, tails diary, verifies a paper trade appears in the mock control plane's DB, tears down. Runs in CI.

## 17. Observability

- Structured JSON logs to stdout (Docker captures them). Fields: `ts, level, mode, org_id, component, message, extra`.
- Localhost Prometheus endpoint `/metrics` on port 9877: heartbeat latency, mode gauge, diary replication lag, live orders/min, paper orders/min, LLM call count, errors.
- Sentry for uncaught exceptions, with `org_id` tag. Sentry DSN comes from config bundle so customers can opt out.

## 18. Security Review Checklist

- [ ] Agent runs as non-root user `proxialpha` (UID 1000).
- [ ] Only the persistent volume is writable; the rest of the filesystem is read-only via `docker run --read-only` recommended but not enforced (breaks some strategies that write temp files).
- [ ] No secrets in image layers (verified with `docker history`).
- [ ] All broker API keys and private wallet keys come from environment variables or files mounted into `/var/lib/proxialpha/secrets/`. Never embedded in config bundles.
- [ ] License token is stored with 0600 permissions.
- [ ] JWT verification uses RS256 only; reject `none` or `HS256` even if signed correctly.
- [ ] `doctor` bundle passes secret-scan test before release.
- [ ] Image is signed with cosign and the public key is published on the website.

## 19. Rollout Plan

1. Cut `proxialpha-agent` as a Python package in the existing repo.
2. Build the Docker image; push to a private registry.
3. Wire up mock control-plane endpoints in a test harness; run smoke end-to-end.
4. Invite Pavan + 2 internal testers to run the agent with real Alpaca paper credentials for 1 week.
5. Stage the release: push to `proxialpha/agent:1.0.0-rc.1`, private beta pulls this tag.
6. After 1 week of beta stability, retag as `proxialpha/agent:1.0.0` and `latest`.
7. Announce in the dashboard "Deploy Agent" page.

## 20. Open Questions

- **Alpine vs Debian-slim base:** Alpine saves ~150MB but complicates pandas/numpy builds. Stick with Debian-slim unless the size becomes a problem.
- **GPU passthrough:** Topology-C customers running vLLM in the same container are out of scope for v1. They run vLLM separately and the agent talks to it over the network.
- **Multi-tenancy inside one agent:** Not supported. One customer per agent. If a customer wants to run multiple strategies as separate users, that's a Team-tier feature deferred to Phase 4.
- **Config bundle schema evolution:** `version: 2` is the initial version. Add `min_agent_version` to the bundle so the control plane can refuse to send a new bundle to an old agent that wouldn't understand it.
