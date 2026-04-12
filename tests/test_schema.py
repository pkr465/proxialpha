"""Schema-level tests for the Phase 1 + Phase 2 initial migration.

These tests are intentionally database-driven (not SQLAlchemy ORM). They
assume ``alembic upgrade head`` has already been run against the target
DB — ``DATABASE_URL`` is read from the environment the same way the app
reads it.

The tests cover the three acceptance criteria from
``docs/prompts/01-db-schema-and-migrations.md``:

1. Every table in the ``public`` schema has RLS enabled.
2. Writes made as org A are invisible when reading as org B.
3. All expected tables exist with the expected columns.

Run with:

    DATABASE_URL=postgresql+asyncpg://localhost/proxialpha_test \\
        pytest tests/test_schema.py -v
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# The set of tables we expect in the ``public`` schema after
# ``alembic upgrade head``. Keep this in lock-step with the migration.
EXPECTED_PUBLIC_TABLES: set[str] = {
    "organizations",
    "users",
    "subscriptions",
    "entitlements",
    "usage_events",
    "agents",
}

# (table, column) pairs that must exist. We check a representative subset
# of each table — enough to catch the kind of mistake where a migration
# renames a column or drops one by accident, without turning this into a
# full DDL dump.
EXPECTED_COLUMNS: dict[str, set[str]] = {
    "organizations": {
        "id",
        "name",
        "stripe_customer_id",
        "tier",
        "created_at",
        "updated_at",
    },
    "users": {"id", "email", "org_id", "role", "clerk_user_id", "created_at"},
    "subscriptions": {
        "id",
        "org_id",
        "stripe_subscription_id",
        "stripe_price_id",
        "status",
        "tier",
        "seats",
        "current_period_start",
        "current_period_end",
        "cancel_at_period_end",
    },
    "entitlements": {
        "id",
        "org_id",
        "feature",
        "period_start",
        "period_end",
        "included",
        "remaining",
        "overage_enabled",
    },
    "usage_events": {
        "id",
        "org_id",
        "feature",
        "quantity",
        "cost_usd",
        "billed",
        "idempotency_key",
        "stripe_usage_record_id",
        "occurred_at",
        "reported_at",
    },
    "agents": {
        "id",
        "org_id",
        "fingerprint",
        "hostname",
        "version",
        "topology",
        "mode",
        "license_jti",
        "grace_until",
        "last_heartbeat_at",
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def database_url() -> str:
    """Resolve the DB URL from the same place the app does."""
    # Import inside the fixture so a missing pydantic-settings doesn't
    # abort test collection for unrelated files.
    from core.settings import get_settings

    get_settings.cache_clear()
    url = get_settings().database_url
    if "asyncpg" not in url:
        pytest.skip(
            "DATABASE_URL must use the postgresql+asyncpg driver for these tests."
        )
    return url


# P2-3: pytest-asyncio 0.23+ enforces an explicit decorator on async
# fixtures in strict mode. Plain ``@pytest.fixture`` on an async-def
# silently produces an unhandled "async fixture" warning that became a
# hard error in pytest 9 + pytest-asyncio 1.x. Decorate explicitly.
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine(database_url: str):
    eng = create_async_engine(database_url, pool_pre_ping=True)
    # Probe the connection at fixture-setup time so a CI environment
    # without a Postgres instance produces a clean ``skip`` instead of
    # eleven identical ``ConnectionRefusedError`` failures (one per
    # parametrised test). The probe runs the cheapest possible query.
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        await eng.dispose()
        pytest.skip(f"Postgres not reachable for schema tests: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def session(engine) -> AsyncSession:
    """Yield a fresh AsyncSession with no tenant context set."""
    async with AsyncSession(engine, expire_on_commit=False) as s:
        yield s


async def _set_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Set ``app.current_org_id`` for the current transaction."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :oid, true)"),
        {"oid": str(org_id)},
    )


# ---------------------------------------------------------------------------
# Test 1 — every public table has RLS enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_public_tables_have_rls_enabled(session: AsyncSession) -> None:
    """Every table in ``public`` must have ``relrowsecurity = true``.

    This is the CI gate promised by ADR-005: a new table that forgets
    to enable RLS fails this test and cannot merge.
    """
    result = await session.execute(
        text(
            """
            SELECT c.relname, c.relrowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind = 'r'
            ORDER BY c.relname;
            """
        )
    )
    rows = result.all()
    assert rows, "no tables found in public schema — did alembic upgrade run?"

    tables_without_rls = [name for (name, rls) in rows if not rls]
    assert tables_without_rls == [], (
        f"RLS not enabled on public tables: {tables_without_rls}. "
        "Every tenant-scoped table must `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`."
    )


# ---------------------------------------------------------------------------
# Test 2 — billing_raw.stripe_events exists and deliberately has NO RLS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stripe_events_exists_without_rls(session: AsyncSession) -> None:
    """``billing_raw.stripe_events`` is the one table that skips RLS by design."""
    result = await session.execute(
        text(
            """
            SELECT c.relrowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'billing_raw'
              AND c.relname = 'stripe_events';
            """
        )
    )
    row = result.first()
    assert row is not None, "billing_raw.stripe_events table missing"
    assert row[0] is False, (
        "billing_raw.stripe_events should NOT have RLS enabled — "
        "it is accessed only by background workers."
    )


# ---------------------------------------------------------------------------
# Test 3 — expected tables and columns exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expected_public_tables_exist(session: AsyncSession) -> None:
    result = await session.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
    )
    present = {row[0] for row in result.all()}
    missing = EXPECTED_PUBLIC_TABLES - present
    assert not missing, f"missing public tables: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("table,expected_cols", sorted(EXPECTED_COLUMNS.items()))
async def test_expected_columns_exist(
    session: AsyncSession, table: str, expected_cols: set[str]
) -> None:
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t"
        ),
        {"t": table},
    )
    present = {row[0] for row in result.all()}
    missing = expected_cols - present
    assert not missing, f"{table} missing columns: {missing}"


# ---------------------------------------------------------------------------
# Test 4 — cross-tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_tenant_reads_are_isolated(engine) -> None:
    """Write as org A; read as org B; assert zero rows."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()

    # We need to write rows bypassing RLS for setup. The easiest portable
    # way is to use a single session that sets the GUC, because the RLS
    # policy uses current_setting() and a matching org_id insert is
    # allowed through the WITH CHECK clause.
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await s.begin()
        await _set_org(s, org_a)
        await s.execute(
            text(
                "INSERT INTO organizations (id, name, tier) "
                "VALUES (:id, 'Org A', 'trader')"
            ),
            {"id": str(org_a)},
        )
        await s.execute(
            text(
                "INSERT INTO entitlements "
                "(org_id, feature, period_start, period_end, included, remaining) "
                "VALUES (:oid, 'signals', now(), now() + interval '30 days', 500, 500)"
            ),
            {"oid": str(org_a)},
        )
        await s.commit()

    async with AsyncSession(engine, expire_on_commit=False) as s:
        await s.begin()
        await _set_org(s, org_b)
        ent_rows = (
            await s.execute(text("SELECT COUNT(*) FROM entitlements"))
        ).scalar_one()
        org_rows = (
            await s.execute(text("SELECT COUNT(*) FROM organizations"))
        ).scalar_one()
        await s.commit()

    assert ent_rows == 0, (
        f"cross-tenant read returned {ent_rows} entitlements rows; "
        "expected 0. RLS policy is not filtering correctly."
    )
    assert org_rows == 0, (
        f"cross-tenant read returned {org_rows} organizations rows; "
        "expected 0. org_self_read policy is not filtering correctly."
    )

    # And sanity check: reading as org A DOES see the row.
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await s.begin()
        await _set_org(s, org_a)
        ent_rows = (
            await s.execute(text("SELECT COUNT(*) FROM entitlements"))
        ).scalar_one()
        await s.commit()

    assert ent_rows == 1, (
        "org A should see its own entitlements row after setting "
        "app.current_org_id = org_a.id"
    )

    # Cleanup — delete as org A (the only org that can see the rows).
    async with AsyncSession(engine, expire_on_commit=False) as s:
        await s.begin()
        await _set_org(s, org_a)
        await s.execute(
            text("DELETE FROM entitlements WHERE org_id = :oid"),
            {"oid": str(org_a)},
        )
        await s.execute(
            text("DELETE FROM organizations WHERE id = :oid"),
            {"oid": str(org_a)},
        )
        await s.commit()


# ---------------------------------------------------------------------------
# Test 5 — entitlements UNIQUE (org_id, feature, period_start) exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entitlements_unique_constraint(session: AsyncSession) -> None:
    """The Phase 1 upsert flow depends on this exact unique key."""
    result = await session.execute(
        text(
            """
            SELECT COUNT(*)
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            WHERE t.relname = 'entitlements'
              AND c.contype = 'u'
              AND pg_get_constraintdef(c.oid) ILIKE
                  '%UNIQUE%(org_id, feature, period_start)%';
            """
        )
    )
    count = result.scalar_one()
    assert count == 1, (
        "entitlements is missing its UNIQUE (org_id, feature, period_start) "
        "constraint — Phase 1 billing upserts will break."
    )
