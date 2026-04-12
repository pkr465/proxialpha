# Task 01 — Database Schema and Alembic Migrations

**Phase:** 1 (Entitlements + Billing)
**Est. effort:** 4–6 hours
**Prerequisites:** Postgres 15+ available locally or in Docker; Python env with `sqlalchemy`, `alembic`, `psycopg[binary]`, `asyncpg`.

## Objective

Create the initial Postgres schema for the ProxiAlpha control plane (`organizations`, `users`, `subscriptions`, `entitlements`, `usage_events`, `agents`, `billing_raw.stripe_events`) with Row-Level Security policies per ADR-005 and a working Alembic setup.

## Context

- This is the first database migration in the project. There is currently **no** `migrations/` or `alembic/` folder in the repo.
- The full schema spec is in `docs/specs/phase1-entitlements-and-billing.md` §5 and `docs/specs/phase2-customer-agent.md` §5.
- Multitenancy pattern is defined in `docs/adr/ADR-005-multitenancy.md` — every tenant-scoped table needs `org_id`, an index on it, RLS enabled, and a default policy using `current_setting('app.current_org_id', true)::uuid`.
- The agent registry table (`agents`) comes from Phase 2 PRD §5 — include it now so Phase 2 doesn't need another migration.

## Exact files to create or modify

1. `alembic.ini` — at repo root. Standard Alembic config pointing to `migrations/`.
2. `migrations/env.py` — standard async Alembic env. Read DB URL from `settings.database_url`.
3. `migrations/versions/20260411_0001_initial_schema.py` — the initial migration.
4. `core/db.py` — new file. Async SQLAlchemy engine factory that sets `SET LOCAL app.current_org_id` from a context variable before every tenant-scoped transaction. See `docs/adr/ADR-005-multitenancy.md` §"Middleware pattern (canonical)".
5. `core/settings.py` — add `database_url` field (pydantic-settings).

Do **not** touch existing files in `api/`, `live_trading/`, `paper_trading/`, `backtesting/`, or `core/`. This task is purely additive to set up the DB foundation.

## Acceptance criteria

- `alembic upgrade head` against a clean Postgres DB creates all tables without error.
- `alembic downgrade base` removes all tables and schemas without error.
- All tenant-scoped tables (`organizations`, `users`, `subscriptions`, `entitlements`, `usage_events`, `agents`) have:
  - `org_id uuid NOT NULL` (except `organizations` itself, which uses `id` as the tenant key)
  - Index on `(org_id, ...)` where relevant
  - `ALTER TABLE ... ENABLE ROW LEVEL SECURITY;`
  - A policy named `tenant_isolation` filtering on `current_setting('app.current_org_id', true)::uuid`
- `billing_raw.stripe_events` exists in its own schema, **does not** have RLS.
- A pytest file at `tests/test_schema.py` verifies:
  - Every table in the `public` schema has RLS enabled (query `pg_tables` and `pg_class.relrowsecurity`)
  - Writing a row as one org and reading it with a different `app.current_org_id` returns no rows
  - The tables listed above all exist and match the column names in the spec
- `core/db.py` exposes `get_session(org_id: uuid.UUID)` as an async context manager that sets the session variable.

## Do not

- Do not install `sqlmodel` — use plain `sqlalchemy` 2.x with async.
- Do not add any seed data in the migration. Seeding happens in a separate task.
- Do not write FastAPI routes, models, or business logic. This task is schema only.
- Do not commit any secrets. `alembic.ini` must read DB URL from env via `core/settings.py`.

## Hints and gotchas

- `current_setting('app.current_org_id', true)` — the `true` makes it return NULL instead of raising when the variable isn't set. This is what you want; the policy becomes `org_id = NULL` which matches nothing.
- To bypass RLS in background workers, connect as a role with `BYPASSRLS`. Don't try to disable RLS per-session — it's a footgun.
- Alembic with async: use `sqlalchemy.ext.asyncio.create_async_engine` and the `asyncio` hook in `env.py`. Reference: https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
- Enum-like fields (tier, status) are plain `text` with `CHECK` constraints — don't use PG enums because they are painful to evolve.
- The `UNIQUE (org_id, feature, period_start)` constraint on `entitlements` is load-bearing; Phase 1 depends on it for upsert semantics.

## Test command

```bash
# From repo root
createdb proxialpha_test
DATABASE_URL=postgresql+asyncpg://localhost/proxialpha_test alembic upgrade head
DATABASE_URL=postgresql+asyncpg://localhost/proxialpha_test pytest tests/test_schema.py -v
DATABASE_URL=postgresql+asyncpg://localhost/proxialpha_test alembic downgrade base
dropdb proxialpha_test
```

All commands must succeed.
