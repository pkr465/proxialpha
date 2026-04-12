# ADR-005: Multitenancy Strategy

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Pavan
**Depends on:** ADR-001, ADR-002

---

## Context

The control plane stores data for every customer: organizations, users, subscriptions, entitlements, usage events, agent registrations, cached diary entries, LLM request logs, and strategy configs. Some of this is sensitive (strategy definitions, trade diary), some is billing-critical (entitlements, usage events). A data leak across orgs would be catastrophic — not just commercially but legally, since a strategy leak could be construed as front-running information.

We need a multitenancy model that:
1. Makes cross-org data access difficult by construction, not just by discipline.
2. Keeps operational complexity low for a small team.
3. Scales to at least 10,000 orgs without architectural re-work.
4. Leaves a clear upgrade path to stronger isolation (per-tenant schema, per-tenant DB) for enterprise customers who demand it.

## Decision

**Shared Postgres, shared schema, every tenant-scoped table has `org_id` as the first column, and Postgres Row-Level Security (RLS) is on by default. The control-plane API sets a per-request `app.current_org_id` session variable before any query, and all policies filter on it.**

- Default deny: RLS is enabled on every table containing tenant data. A request without `app.current_org_id` set sees nothing.
- The `authenticate` middleware in FastAPI extracts `org_id` from the session JWT and runs `SET LOCAL app.current_org_id = $1` inside the transaction.
- A small set of tables is explicitly *not* RLS-scoped and lives in a separate schema: `billing_raw.stripe_events`, `observability.logs`, `system.feature_flags`. These are accessed only by background workers with elevated credentials.
- Agent-originated requests carry the license token (ADR-003) which has its own `org_id` claim; the agent-handler extracts it and applies the same session variable.

## Options Considered

### Option 1: Separate database per tenant

- **Pros:** Strongest isolation. Easy to satisfy "we want our data in our own DB" enterprise demands.
- **Cons:**
  - Ops nightmare at scale — 10,000 Postgres instances is not operable by a small team.
  - Migrations become a fan-out problem with retries and backpressure.
  - Cross-tenant aggregations (our own analytics) become much harder.
- **Verdict:** Rejected for v1. Retained as an option for a future "dedicated instance" SKU.

### Option 2: Separate schema per tenant in shared DB

- **Pros:** Strong logical isolation. Backups are tenant-scoped.
- **Cons:**
  - Postgres connection pool churn — search_path per session is workable but fiddly.
  - Migrations must run once per schema. At 1,000+ tenants this becomes a real job.
  - Query plans and statistics don't share across schemas, which increases planner overhead.
- **Verdict:** Rejected for v1. Reconsider if a single enterprise tenant needs stronger isolation than RLS offers.

### Option 3 (Chosen): Shared schema + Row-Level Security

- **Pros:**
  - Single migration step; single connection pool; single backup.
  - RLS is enforced at the database layer — even an SQL injection in application code cannot read across tenants, assuming the session variable is set by trusted middleware and not by user input.
  - Well-trodden pattern (Supabase, PostHog, Linear, many others).
- **Cons:**
  - Relies on discipline: every new table must have `org_id` and an RLS policy. A missed policy is a silent leak.
  - Mitigation: a test in the CI suite that asserts every table in the `public` schema either has `org_id + enabled RLS` or is in an allowlist.
  - Observability: slow queries for a single tenant can degrade everyone. Mitigation: per-org query budget via pg_stat_statements and alerting.
- **Verdict:** Chosen.

### Option 4: Application-layer tenant filter (no RLS)

- **Pros:** Simplest code.
- **Cons:** A single missed WHERE clause leaks data. The failure mode is silent. RLS is only a few extra lines and buys a meaningfully stronger guarantee.
- **Verdict:** Rejected. Defense in depth is cheap here.

## Consequences

### Positive

- New tables automatically inherit the pattern: `org_id uuid NOT NULL`, indexed, RLS enabled, a policy of `org_id = current_setting('app.current_org_id')::uuid`.
- A SQL injection in any query that forgets WHERE can still only leak the current tenant's rows, not cross-tenant rows. This is a dramatic reduction in blast radius.
- Auditing is centralized: all cross-tenant queries (billing rollups, analytics) run as a dedicated `bg_worker` role that bypasses RLS explicitly via `SET row_security = off`, and these queries are logged.

### Negative

- Every table migration must include the RLS-enable step. We will ship a CI check that fails the build if a new public-schema table lacks RLS or a policy.
- Raw SQL debugging is slightly harder: `psql` as an app user sees nothing by default; operators need a dedicated `dba` role that sets `row_security = off` explicitly.
- Postgres RLS has a small performance cost (sub-millisecond for indexed queries; measurable on large analytical scans). Acceptable for a transactional workload.

### Migration pattern (canonical)

```sql
CREATE TABLE entitlements (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  feature       text NOT NULL,
  period_start  timestamptz NOT NULL,
  period_end    timestamptz NOT NULL,
  included      bigint NOT NULL DEFAULT 0,
  remaining     bigint NOT NULL DEFAULT 0,
  overage_enabled boolean NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, feature, period_start)
);

CREATE INDEX idx_entitlements_org_feature ON entitlements (org_id, feature);

ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON entitlements
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

### Middleware pattern (canonical)

```python
# api/middleware/tenant.py
async def set_tenant_context(request: Request, call_next):
    org_id = request.state.user.org_id  # set by auth middleware
    async with db.transaction():
        await db.execute(
            "SELECT set_config('app.current_org_id', :org_id, true)",
            {"org_id": str(org_id)},
        )
        return await call_next(request)
```

`set_config(..., true)` makes the setting transaction-local, so connection pooling is safe.

### Test pattern (canonical)

```python
# tests/test_tenant_isolation.py
def test_cross_tenant_query_returns_empty(db):
    org_a = create_org()
    org_b = create_org()
    create_entitlement(org_a, "signals", 500)

    with tenant_context(org_b.id):
        rows = db.fetch_all("SELECT * FROM entitlements")
    assert rows == []
```

Every new tenant-scoped feature must ship with a test like this.

## Open Questions

1. **Dedicated instance SKU:** When a Team-tier customer says "we want our own database," we offer a higher-priced "Dedicated" plan that spins up a separate DB. Deferred until a customer actually asks.
2. **Soft-delete vs hard-delete of orgs:** Stripe says a cancelled subscription is cancelled; we keep the org row with `status='cancelled'` for 90 days for winback, then hard-delete via cascade. Exact retention period to be confirmed with legal during Phase 5.
3. **Cross-org admin tools:** Internal support staff will need a read-only escalation path. Implement as an explicit `impersonation_session` table with expiring tokens, logged to an audit log, visible to the customer org on their dashboard.
