# Control plane deployment runbook

This runbook covers a clean production deploy of the ProxiAlpha
control plane (FastAPI app + alembic migrations + the meter_usage
cron). It does not cover the customer-facing trading agent — that
ships through a separate pipeline (`Dockerfile.agent`).

## Prerequisites

The control plane needs the following provisioned BEFORE the first
deploy:

1. **Postgres 16+**, with the `pgcrypto` extension available. AWS
   RDS, GCP Cloud SQL, Fly.io Postgres, and Supabase all qualify.
   Connection string format:
   `postgresql+asyncpg://USER:PASS@HOST:5432/DB`.

2. **Stripe account**, with:
   - A live secret key (`sk_live_...`).
   - A webhook endpoint pointing at `https://<your-host>/api/billing/webhook`.
     The endpoint signing secret (`whsec_...`) goes into
     `STRIPE_WEBHOOK_SECRET`.
   - The product / price catalog published per `docs/specs/phase1-entitlements-and-billing.md`.

3. **Clerk instance**, with:
   - JWT issuer URL (e.g. `https://your-instance.clerk.accounts.dev`).
   - The dashboard origin allow-listed in Clerk's settings.
   - A JWT template that emits `org_id`, `email`, and `sub` claims.

4. **Agent signing key**:
   - Generate with `openssl genrsa -out agent_signing_key.pem 2048`.
   - Store in your secret manager (AWS Secrets Manager, GCP Secret
     Manager, K8s Secret, …) and mount into the container at a
     filesystem path. Set `AGENT_SIGNING_KEY_PATH` to that path.
   - The corresponding **public** key is what the customer agent
     binary embeds. Generate it with
     `openssl rsa -in agent_signing_key.pem -pubout -out agent_signing_key.pub`
     and check it into the agent repo (it's not a secret).

5. **DNS**:
   - `api.proxiant.io` → control plane
   - `app.proxiant.io` → dashboard frontend (separate deploy)

## Required environment variables

| Variable | Required? | Notes |
|----------|-----------|-------|
| `DATABASE_URL` | yes | `postgresql+asyncpg://...` |
| `ALEMBIC_DATABASE_URL` | yes | `postgresql+psycopg2://...` (sync driver, alembic only) |
| `STRIPE_SECRET_KEY` | yes | `sk_live_...` |
| `STRIPE_WEBHOOK_SECRET` | yes | `whsec_...` |
| `APP_URL` | yes | dashboard URL, used for Stripe Portal return |
| `CONTROL_PLANE_PUBLIC_URL` | yes | this service's own URL, for JWKS |
| `CORS_ALLOWED_ORIGINS` | yes | CSV — set to dashboard origin |
| `CLERK_ISSUER` | yes | empty disables Clerk auth (DEV ONLY) |
| `CLERK_REQUIRE_TOKEN` | yes | set to `1` in prod to disable stub headers |
| `AGENT_SIGNING_KEY_PATH` | yes | path inside the container |
| `BG_WORKER_PASSWORD` | yes | password for the bg_worker DB role |
| `ENV` | yes | `prod` |
| `ENTITLEMENTS_ENABLED` | optional | defaults to on |
| `SIGNING_KEY_PROVIDER` | optional | `file` (default) — KMS providers TBD |

## Deploy sequence

1. **Build the image**
   ```
   docker build -f deploy/Dockerfile.controlplane -t proxialpha-controlplane:$(git rev-parse --short HEAD) .
   ```
   Tag and push to your registry.

2. **Run migrations** (once per release; idempotent)
   ```
   docker run --rm \
     -e ALEMBIC_DATABASE_URL=$ALEMBIC_DATABASE_URL \
     -e BG_WORKER_PASSWORD=$BG_WORKER_PASSWORD \
     proxialpha-controlplane:$(git rev-parse --short HEAD) \
     alembic upgrade head
   ```
   Always run this BEFORE rolling the API pods. The schema is
   forward-compatible with the previous app version, so the API can
   tolerate the migration running first; the reverse is not safe.

3. **Roll the API pods** (rolling restart, 1-by-1 with health checks)
   - The container's `HEALTHCHECK` hits `/api/health` every 15s.
   - Pods are ready when the JWKS endpoint also serves 200:
     ```
     curl -fsS https://api.proxiant.io/.well-known/jwks.json
     ```

4. **Verify the cron**
   - The meter_usage job is scheduled separately (k8s CronJob, ECS
     Scheduled Task, Fly.io machine-on-cron, …) at hourly cadence.
   - Manual smoke test: `python -m jobs.meter_usage`. Should exit 0
     and log "advisory-lock acquired" once.

5. **Smoke test the agent path**
   - Issue an install token from the dashboard.
   - On a sandbox machine, run the customer agent with that token
     against the new control plane.
   - Confirm the agent's first heartbeat shows up in the
     `agents` table.

## Rollback

If a deploy goes bad after step 3:

1. Re-deploy the previous image tag. The migration in step 2 was
   forward-only; almost all schema changes in this codebase are
   additive, so the previous app version still runs against the new
   schema. The Phase 2 migrations in `migrations/versions/0004` and
   `0005` are both additive (new tables, new column).

2. If the migration ITSELF caused the failure, run
   `alembic downgrade -1`. Note this will drop the new tables —
   verify that no production traffic has written rows that would be
   destroyed.

3. Stripe webhook IDs survive across versions (the
   `billing_raw.stripe_events` ledger is the source of truth), so a
   rollback never loses webhook data.

## Observability checklist

After every deploy, confirm:

- `GET /api/health` returns 200 with `status=ok`.
- `GET /.well-known/jwks.json` returns 200 with at least one key.
- The first heartbeat from a real agent shows up in the
  `heartbeat_jti_seen` table within 1 hour of cutover.
- No ERROR-level log lines in the API container in the first hour.

## See also

- `docs/runbooks/signing-key-rotation.md` — how to rotate the agent
  signing key without invalidating the field fleet.
- `docs/runbooks/incident-license-revocation.md` — how to revoke a
  compromised agent without waiting for a key rotation.
- `docs/specs/phase2-go-live-gap-analysis.md` — the original
  blocker list this deploy closes.
