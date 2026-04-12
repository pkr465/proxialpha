# ProxiAlpha — End-to-End Go-Live Gap Analysis

**Date:** 2026-04-11
**Scope:** Phase 1 (control plane / billing / entitlements / heartbeat) + Phase 2 (customer agent image + CI)
**Verdict:** **NOT shippable as-is.** There are five P0 blockers that will prevent a single real customer from enrolling, plus a longer list of P1/P2 items that make the difference between "a demo" and "a system an ops team can run on-call."

This document is organized by severity. Every gap cites the file(s) it lives in so the fix can be scoped and assigned. Severity is defined as:

- **P0 — Blocker.** Go-live is impossible until this is closed. An agent cannot enroll, or a customer's data can be read by another customer, or the service cannot start.
- **P1 — Ship-stopper for production.** The system will appear to work in staging but will fail, leak, or be unauditable in production within days.
- **P2 — Must-have for scale / on-call.** The team will page each other at 3am if this isn't done, but the service will run.
- **P3 — Nice to have.** Deferred with acknowledged debt.

---

## P0 — Blockers (must fix before any customer touches this)

### P0-1. The `/agent/enroll` endpoint does not exist on the control plane
**Files:** `api/agent/__init__.py`, `api/agent/heartbeat.py`, `proxialpha_agent/license.py`
**Impact:** No agent can ever onboard. The Phase 1 ↔ Phase 2 wire is severed.

The agent's `license.py` posts to `{control_plane_url}/agent/enroll` with its install token and fingerprint to receive its first signed license JWT. The server has `agent_router` wired, but `agent_router` only `include_router`s `_heartbeat_router`. A grep for `enroll` across `api/` returns zero hits. The endpoint has never been implemented.

**Fix:** Add `api/agent/enroll.py` with `POST /agent/enroll`:
1. Validate `install_token` against `install_tokens` table (table also missing — see P0-4).
2. Upsert `agents` row keyed by `(org_id, fingerprint)`.
3. Issue an RS256-signed license JWT with 24h lifetime, topology, entitlements claim.
4. Return `{license_jwt, jwks_url, heartbeat_interval_seconds}`.

This is the single most important missing piece. Nothing else Phase 2 does matters if an agent cannot get a first license.

---

### P0-2. There is no real authentication — `AuthStubMiddleware` is still the only auth layer
**Files:** `api/middleware/auth_stub.py`, `api/server.py`
**Impact:** Any HTTP client can masquerade as any org by setting an `X-Org-Id` header. Full cross-tenant data breach on day one.

The middleware's own module docstring says:

> `TODO(phase1-task4): Replace this entire module with a real Clerk JWT verifier. Verify the signature against Clerk's JWKS endpoint (cached). Look up our internal users and organizations rows by Clerk ID, creating them on first sight (Clerk JIT-provisioning).`

That task was never done. RLS on the Postgres side is useless when the application layer trusts a client header to set `app.current_org_id`.

**Fix:** Ship a Clerk JWT verifier as a FastAPI dependency. Cache JWKS for 10 minutes. On first sight of a `clerk_user_id`/`clerk_org_id` pair that isn't in our DB, JIT-provision an `organizations` + `users` row. Set `app.current_org_id` from the verified claim, not the header. Delete `auth_stub.py` entirely — do not leave a feature flag toggle to it.

---

### P0-3. There is no JWKS endpoint — ADR-003's key rotation story is unshippable
**Files:** `core/jwt_keys.py`, `api/server.py`, `docs/adr/ADR-003-license-token-and-heartbeat.md`
**Impact:** The agent trusts a single public key baked into its image. When the key has to rotate (lost laptop, leaked env var, 90-day compliance rotation), every agent in the field has to be reimaged. That is not an incident response plan, that is an outage.

ADR-003 §Key Distribution explicitly calls for `GET /.well-known/jwks.json` with the current + previous signing keys in the JWKS set, cached with a 10-minute TTL on the agent side. `core/jwt_keys.py` even has a comment saying "it'll land in Task 07 alongside a JWKS endpoint." Task 07 shipped the signing side but never the JWKS side.

**Fix:** 
1. Add `GET /.well-known/jwks.json` returning the current and previous public keys as a JWK set with stable `kid` values.
2. Add a fetch-and-cache helper in `proxialpha_agent/license.py` that falls back to JWKS when the embedded key's `kid` doesn't match the token's `kid`.
3. Add a runbook for rotation: generate new key → upload → flip active pointer → old key stays in JWKS for 48h → remove.

---

### P0-4. `pyproject.toml` does not declare the control plane's own dependencies
**Files:** `pyproject.toml`, `requirements.txt`
**Impact:** `pip install .` on a fresh machine will not produce a runnable control plane. CI passes today only because the test runner manually `pip install`s each dep by name.

The control plane imports `alembic`, `sqlalchemy`, `asyncpg`, `psycopg`, and `stripe`. None are in `pyproject.toml`'s `[project.dependencies]`. `requirements.txt` is still the legacy trading-engine list (yfinance, pandas, numpy) and has no control-plane entries. `pyjwt` and `cryptography` are listed transitively but not explicitly.

Related: `[tool.setuptools.packages.find]` enumerates `proxialpha_agent`, `proxialpha_agent.keys`, `core`, `api`, `api.agent` — it **excludes** `api.billing`, `api.middleware`, and `jobs/`. A wheel built from this repo today would be silently missing the entitlements router, the auth middleware, and the metering cron.

**Fix:**
1. Add to `[project.dependencies]`: `alembic>=1.13`, `sqlalchemy[asyncio]>=2.0`, `asyncpg>=0.29`, `psycopg[binary]>=3.1`, `stripe>=8.0`, and hoist `pyjwt[crypto]>=2.8`, `cryptography>=42.0`, `fastapi>=0.110`, `uvicorn[standard]>=0.27`.
2. Extend `packages` to include `api.billing`, `api.middleware`, `jobs`.
3. Split `requirements.txt` → `requirements-agent.txt` (thin) and `requirements-control-plane.txt` (full), or delete it entirely in favor of `pyproject.toml` extras.

---

### P0-5. The `bg_worker` role's password is the literal string `'CHANGE_ME'` in a committed migration
**Files:** `migrations/versions/20260411_0002_bg_worker_role.py`
**Impact:** If that migration is ever applied to a production database, the metering cron's DB role has a guessable password. It also means `CHANGE_ME` is now in the git history forever — rotating it later requires a new migration that drops and recreates the role.

**Fix:** Replace the literal with `os.environ["BG_WORKER_ROLE_PASSWORD"]` resolved at migration runtime, or — better — use `ALTER ROLE bg_worker PASSWORD :password` via a parameterized exec and fail-closed if the env var is missing. Document in `deploy/cron.md` that the password must be set in the Alembic invocation's env.

---

## P1 — Ship-stoppers for production

### P1-1. CORS is `allow_origins=["*"]`
**File:** `api/server.py`
The FastAPI app currently trusts every origin with credentials. Combined with the auth stub (P0-2) this is a self-serve tenant dump. Fix alongside P0-2: allow only the dashboard origin from config.

### P1-2. `ENTITLEMENTS_ENABLED` is off by default
**File:** `api/server.py`, `api/billing/entitlements.py` (or wherever the flag lives)
The entitlements gate is behind a feature flag that defaults to `0`. The flag should be on by default; off should require an explicit `ENTITLEMENTS_ENABLED=0` for local dev only. Ship with the gate closed.

### P1-3. Legacy trading paths are not wired to the entitlements gate
**Files:** `strategies/`, `live_trading/`, `paper_trading/`, `api/server.py`
The entitlement gate is applied to `/api/llm/analyze` but not to the trade execution path. An entitled-for-paper-only customer can today hit a live-trading endpoint and it will not be blocked at the API layer. Audit every legacy router and either wrap it with the entitlements dependency or remove it from the deployed image.

### P1-4. No install-token issuance flow
**Files:** (missing) `api/billing/install_tokens.py`, dashboard UI
Even once P0-1 is fixed, there is no way for an admin to generate an install token to give to the customer-side operator. The enroll endpoint will validate tokens from a table that nothing writes to. Fix: add `POST /api/orgs/{org_id}/install-tokens` (Clerk-authed, returns single-use 10-minute-TTL token) + show it once in the dashboard.

### P1-5. No signing-key storage beyond a PEM on disk
**Files:** `core/jwt_keys.py`
The agent signing key is loaded from `AGENT_SIGNING_KEY_PATH` or `AGENT_SIGNING_KEY_PEM`. There is no KMS or Vault integration, no envelope encryption, no HSM. For a system whose entire trust model is "we sign every license," this is thin. Minimum bar: load from AWS KMS / GCP KMS with a local file fallback only in dev.

### P1-6. No replay protection on heartbeat `jti`
**File:** `api/agent/heartbeat.py`
ADR-003 §Security is explicit: "We do not accept the same `jti` twice for heartbeat purposes." The heartbeat handler needs a Redis `SETNX` (or a dedicated table with a unique index on `jti` and a background pruner) to enforce it. Without this, a stolen heartbeat can be replayed indefinitely and the "last seen" telemetry can be forged.

### P1-7. No emergency revocation path
**Files:** (missing)
If an install token is leaked or an agent goes rogue, there is no way to revoke it short of rotating the signing key. Add a `revoked_jti` table (or bloom filter) that is checked on heartbeat and an admin endpoint that writes to it.

### P1-8. No rate limiting on any endpoint
**File:** `api/server.py`
Enroll and heartbeat are both unauthenticated from the agent's point of view until the license is issued. Both are cheap to call and both write to Postgres. Add a `slowapi`-style limiter keyed on client IP + install token hash before go-live. Without it, one badly-configured customer agent can wedge the control plane.

### P1-9. No deployment manifests
**Files:** only `deploy/cron.md` exists
There is no Kubernetes manifest, no Helm chart, no Terraform, no Dockerfile for the control plane itself. The customer agent has a full image + CI pipeline; the control plane it connects to has neither. An on-call engineer has no documented way to deploy or roll back a change. Add at minimum: `Dockerfile.api`, `Dockerfile.worker`, `deploy/helm/` or `deploy/terraform/`, and a top-level `deploy/README.md`.

### P1-10. `jwks_url` setting on the agent has no fetcher
**File:** `proxialpha_agent/license.py`, `proxialpha_agent/settings.py`
The setting exists in `settings.py` but `license.py` never reads it to fetch and cache JWKS. Complementary to P0-3 — the endpoint and the client both need to land together, or neither helps.

---

## P2 — Must-have for scale / on-call

### P2-1. No onboarding / admin dashboard UI
**Files:** `web/index.html`, `web/app.jsx`
The web surface is a placeholder. There's no flow to create an org, invite teammates, generate an install token, view agent health, or see billing usage. Until this exists, every new customer requires an engineer running SQL and curl on the admin's behalf.

### P2-2. No runbooks
**Files:** (missing) `docs/runbooks/`
None of the on-call scenarios are documented: agent enrollment failure, heartbeat flood, Stripe metering outage, DB failover, signing key rotation, Clerk outage. Add a runbook per scenario with exact commands. The existing `deploy/cron.md` is the only ops artifact and it covers one job.

### P2-3. 11 pytest-asyncio deprecation errors in `tests/test_schema.py`
**File:** `tests/test_schema.py`
Pre-existing. pytest-asyncio 9.0 deprecation warnings are now collection errors. These tests haven't run in CI for however long — the effective coverage on the schema layer is zero. Either pin pytest-asyncio<0.24 with a TODO to update, or add `loop_scope="session"` / mode config and fix the tests properly.

### P2-4. No observability
**Files:** `api/server.py`, `jobs/meter_usage.py`, `proxialpha_agent/`
No OpenTelemetry, no structured JSON logging beyond `print()`, no metrics endpoint, no SLO dashboards. First incident will be impossible to triage. Minimum bar: structlog + OTel tracing + a `/metrics` endpoint for Prometheus on both the API and the agent.

### P2-5. No alembic downgrade path tested
**Files:** `migrations/versions/*.py`
Every migration has a stub `downgrade()` but nothing runs them. A bad migration in production cannot be rolled back with any confidence. Add a CI job that runs `alembic upgrade head && alembic downgrade base && alembic upgrade head` against a fresh test database.

### P2-6. Stripe metering job is a single-writer cron with no locking
**File:** `jobs/meter_usage.py`
The header notes it's single-writer by convention. There is no actual lock. If two instances run concurrently (cron drift, a human re-running it), the 5-minute safety window helps but the `action="set"` Stripe semantics alone may not be enough for all feature types. Add a Postgres advisory lock (`pg_try_advisory_lock(hashtext('meter_usage'))`) as job-level belt-and-suspenders.

### P2-7. No doctor bundle ingestion path
**Files:** (missing)
We built `proxialpha doctor` (Task 08) but there's nowhere for a customer to send the bundle. Minimum: a `POST /api/support/bundles` endpoint that uploads to object storage, tags with org_id, and pages the on-call support engineer.

### P2-8. No TLS certificate pinning on the agent
**File:** `proxialpha_agent/license.py`
The agent trusts whatever root CAs are in the runtime image. For a system where the control plane URL is the single source of all authority, pinning the control plane's leaf or intermediate is worth the maintenance cost.

---

## P3 — Debt to acknowledge

- No billing-reconciliation report (Stripe invoices vs. our `usage_events`).
- No backfill script for `usage_events` if the metering job is ever wedged for >1 hour.
- `.trivyignore` is gitignored but no process exists to review ignored CVEs quarterly.
- Agent image builds on `python:3.11-slim-bookworm` with a floating tag — pin the digest for true reproducibility.
- No Dependabot / Renovate config for either the agent or the control plane.
- No SBOM publication step for the control plane (the agent pipeline already emits one).

---

## Recommended sequencing to go-live

**Week 1 — Unblock enrollment (P0 closure).** 
Fix P0-1 (enroll endpoint), P0-2 (Clerk auth), P0-3 (JWKS), P0-4 (pyproject deps), P0-5 (migration secret). After this week, a customer can in principle run through the flow end to end in staging.

**Week 2 — Close the obvious holes (P1-1 → P1-5).** 
CORS, entitlement default-on, legacy trading path gating, install-token issuance, KMS-backed signing. The system is now defensible against a curious attacker, not just a friendly one.

**Week 3 — Operational basics (P1-6 → P1-10 + P2-1, P2-2).**
Heartbeat `jti` replay, revocation list, rate limiting, deployment manifests, agent JWKS fetch, a minimal dashboard, runbooks. An on-call engineer who has never seen the system can now run it.

**Week 4 — Observability and hardening (P2-3 → P2-8).** 
Fix the broken schema tests, add metrics/tracing/structured logs, alembic downgrade CI, advisory lock on metering, doctor bundle upload, TLS pin. You are now live-worthy.

Only after Week 4 should a paying customer be pointed at production.

---

## Sign-off criteria

I will consider Phase 1 + Phase 2 "go-live ready" when, on a fresh dev machine:

1. `pip install .` from `pyproject.toml` alone produces a working control plane and working agent.
2. A new org can be created through the dashboard by a Clerk user with zero SQL.
3. That org's admin can generate an install token, run `docker run ghcr.io/proxialpha/agent:<version>` with the token, and see the agent reach `running` mode within 60s.
4. Rotating the signing key (`ops/rotate_signing_key.sh`) causes every in-field agent to pick up the new key within one JWKS TTL with zero downtime.
5. `cosign verify` of the agent image passes against the documented identity.
6. A deliberate entitlement downgrade on the Stripe side propagates through the control plane and causes the agent to drop out of `running` into `degraded` within one license refresh cycle.
7. A deliberate control-plane outage lasting <7 days leaves the agent in `offline_grace` with paper trading still functional and live trading blocked.
8. `proxialpha doctor` produces a bundle, that bundle is uploaded through the support endpoint, and the redaction self-check passes on the server side too.

Until all eight of those checks pass in staging, we are not ready.
