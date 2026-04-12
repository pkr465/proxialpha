"""Tests for ``core.entitlements`` and ``api.billing.read`` (Task 04).

Covers the atomic consume path, peek, the decorator, the
GET /api/entitlements response shape, and a concurrency test that
drives 20 simultaneous consumers against a 10-unit quota to prove
the SQL is atomic under contention.

SQLite caveats
--------------

These tests run against in-memory SQLite. The module-under-test uses
cross-dialect SQL (see ``core.entitlements._CONSUME_SQL`` for the
reasoning) so the same statement runs in both dialects.

For the concurrency test we swap the default NullPool for
``StaticPool`` so multiple async tasks share a single in-memory DB
connection — otherwise each session would open its own private
in-memory DB and the invariant we want to prove would be vacuous.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Stripe env vars — unused by the entitlements core but imported
# transitively by api.billing (which depends on tiers.yaml being
# happy). Set defaults so imports don't fail.
os.environ.setdefault("STRIPE_PRICE_TRADER_MONTHLY", "price_trader_monthly_xxx")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("APP_URL", "http://localhost:3000")

from api.middleware.auth_stub import AuthStubMiddleware  # noqa: E402
from core import entitlements as ent_module  # noqa: E402


# ---------------------------------------------------------------------------
# Schema — minimal subset that entitlements + read touch
# ---------------------------------------------------------------------------

_SCHEMA_SQL = [
    """
    CREATE TABLE organizations (
        id                 TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        stripe_customer_id TEXT UNIQUE,
        tier               TEXT NOT NULL DEFAULT 'free',
        created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE entitlements (
        id              TEXT PRIMARY KEY,
        org_id          TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        feature         TEXT NOT NULL,
        period_start    TEXT NOT NULL,
        period_end      TEXT NOT NULL,
        included        INTEGER NOT NULL DEFAULT 0,
        remaining       INTEGER NOT NULL DEFAULT 0,
        overage_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (org_id, feature, period_start)
    )
    """,
    """
    CREATE TABLE usage_events (
        id                     TEXT PRIMARY KEY,
        org_id                 TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        feature                TEXT NOT NULL,
        quantity               INTEGER NOT NULL,
        cost_usd               REAL,
        billed                 INTEGER NOT NULL DEFAULT 0,
        idempotency_key        TEXT NOT NULL UNIQUE,
        stripe_usage_record_id TEXT,
        occurred_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        reported_at            TEXT
    )
    """,
]


# Period bounds that are comfortably in the future so the
# ``period_end > :now`` check always passes. Format matches what
# ``_now_iso()`` produces so lex comparison in SQLite is correct.
_PERIOD_START = "2026-01-01T00:00:00+00:00"
_PERIOD_END = "2099-12-31T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures — shared connection (StaticPool) for isolation + concurrency
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    """Fresh SQLite engine per test, shared-connection via StaticPool.

    StaticPool is what lets multiple sessions see the SAME in-memory
    database. Without it, each session opens its own `:memory:` DB
    and no one can see anyone else's data. StaticPool also lets the
    concurrency test exercise many sessions against one connection.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with engine.begin() as conn:
        for stmt in _SCHEMA_SQL:
            await conn.execute(text(stmt))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(db_engine):
    return async_sessionmaker(
        bind=db_engine, expire_on_commit=False, class_=AsyncSession
    )


@pytest_asyncio.fixture
async def seed_org_and_entitlement(db_sessionmaker):
    """Insert an org + one ``signals`` entitlement row.

    Returns a dict with ``org_id``, the tier name, and the function
    that re-reads the current ``remaining`` (useful for assertions).
    Tier is ``trader`` so overage cost lookup has a matching entry.
    """
    org_id = str(uuid.uuid4())

    async def _insert(remaining: int, overage_enabled: bool) -> None:
        async with db_sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO organizations (id, name, tier) "
                    "VALUES (:id, :n, 'trader')"
                ),
                {"id": org_id, "n": "Test Org"},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO entitlements (
                        id, org_id, feature, period_start, period_end,
                        included, remaining, overage_enabled
                    )
                    VALUES (
                        :id, :org_id, 'signals', :p_start, :p_end,
                        :included, :remaining, :overage
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "p_start": _PERIOD_START,
                    "p_end": _PERIOD_END,
                    "included": max(remaining, 0),
                    "remaining": remaining,
                    "overage": 1 if overage_enabled else 0,
                },
            )
            await session.commit()

    async def _read_remaining() -> int:
        async with db_sessionmaker() as session:
            result = await session.execute(
                text(
                    "SELECT remaining FROM entitlements "
                    "WHERE org_id = :org_id AND feature = 'signals'"
                ),
                {"org_id": org_id},
            )
            row = result.fetchone()
            return int(row[0]) if row else -1

    async def _count_usage_events() -> int:
        async with db_sessionmaker() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM usage_events "
                    "WHERE org_id = :org_id AND billed = 1"
                ),
                {"org_id": org_id},
            )
            return int(result.fetchone()[0])

    return {
        "org_id": org_id,
        "insert": _insert,
        "read_remaining": _read_remaining,
        "count_usage_events": _count_usage_events,
    }


# ---------------------------------------------------------------------------
# Unit tests — try_consume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_consume_decrements_atomically(
    db_sessionmaker, seed_org_and_entitlement
):
    """Start at 100, consume 10, end at 90. Allowed=True, not overage."""
    await seed_org_and_entitlement["insert"](remaining=100, overage_enabled=False)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    async with db_sessionmaker() as session:
        result = await ent_module.try_consume(session, org_uuid, "signals", 10)
        await session.commit()

    assert result.allowed is True
    assert result.remaining == 90
    assert result.is_overage is False
    assert await seed_org_and_entitlement["read_remaining"]() == 90


@pytest.mark.asyncio
async def test_try_consume_blocks_when_exhausted_without_overage(
    db_sessionmaker, seed_org_and_entitlement
):
    """remaining=5, consume 10, overage disabled → allowed=False, no write."""
    await seed_org_and_entitlement["insert"](remaining=5, overage_enabled=False)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    async with db_sessionmaker() as session:
        result = await ent_module.try_consume(session, org_uuid, "signals", 10)
        await session.commit()

    assert result.allowed is False
    assert result.remaining == 0  # we intentionally don't leak the real value
    # UPDATE must not have fired — remaining stays at 5.
    assert await seed_org_and_entitlement["read_remaining"]() == 5


@pytest.mark.asyncio
async def test_try_consume_allows_overage_when_enabled(
    db_sessionmaker, seed_org_and_entitlement
):
    """remaining=5, overage enabled, consume 10 → allowed, remaining=-5, is_overage."""
    await seed_org_and_entitlement["insert"](remaining=5, overage_enabled=True)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    async with db_sessionmaker() as session:
        result = await ent_module.try_consume(session, org_uuid, "signals", 10)
        await session.commit()

    assert result.allowed is True
    assert result.remaining == -5
    assert result.is_overage is True


@pytest.mark.asyncio
async def test_try_consume_logs_usage_event_on_overage(
    db_sessionmaker, seed_org_and_entitlement
):
    """Overage path must insert exactly one usage_events row with billed=true."""
    await seed_org_and_entitlement["insert"](remaining=2, overage_enabled=True)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    async with db_sessionmaker() as session:
        result = await ent_module.try_consume(session, org_uuid, "signals", 5)
        await session.commit()

    assert result.is_overage is True
    assert await seed_org_and_entitlement["count_usage_events"]() == 1


@pytest.mark.asyncio
async def test_try_consume_concurrent_does_not_oversell(
    db_sessionmaker, seed_org_and_entitlement
):
    """20 concurrent consumers against remaining=10 — exactly 10 must succeed.

    This is the atomicity acceptance test. If the UPDATE ever allows
    more than 10 successful results, the atomicity invariant is broken
    and the whole entitlement system is unsafe.
    """
    await seed_org_and_entitlement["insert"](remaining=10, overage_enabled=False)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    async def one_call() -> bool:
        async with db_sessionmaker() as session:
            r = await ent_module.try_consume(session, org_uuid, "signals", 1)
            await session.commit()
            return r.allowed

    results = await asyncio.gather(*[one_call() for _ in range(20)])
    assert sum(1 for a in results if a) == 10
    assert await seed_org_and_entitlement["read_remaining"]() == 0


@pytest.mark.asyncio
async def test_peek_is_read_only(db_sessionmaker, seed_org_and_entitlement):
    """100 peeks must not mutate ``remaining``."""
    await seed_org_and_entitlement["insert"](remaining=100, overage_enabled=False)
    org_uuid = uuid.UUID(seed_org_and_entitlement["org_id"])

    for _ in range(100):
        async with db_sessionmaker() as session:
            snap = await ent_module.peek(session, org_uuid, "signals")
        assert snap is not None
        assert snap["remaining"] == 100

    assert await seed_org_and_entitlement["read_remaining"]() == 100


# ---------------------------------------------------------------------------
# Integration tests — decorator via FastAPI
# ---------------------------------------------------------------------------


def _build_decorated_app(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker
) -> FastAPI:
    """Construct a minimal FastAPI app with one decorated route.

    The decorator pulls its session from ``core.entitlements._session_factory``;
    we monkey-patch that to a fixture-backed factory so the route under
    test uses the same SQLite DB the fixtures wrote to.
    """
    from contextlib import asynccontextmanager

    from core.entitlements import requires_entitlement

    @asynccontextmanager
    async def _fake_factory() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr(ent_module, "_session_factory", _fake_factory)

    app = FastAPI()
    app.add_middleware(AuthStubMiddleware)

    @app.post("/gated/signal")
    @requires_entitlement("signals", consume=1)
    async def gated(request: Request, response: Response):
        return {"ok": True}

    return app


@pytest.mark.asyncio
async def test_decorator_returns_402_on_exhaustion(
    db_sessionmaker, seed_org_and_entitlement, monkeypatch
):
    """A gated route with exhausted quota returns 402."""
    await seed_org_and_entitlement["insert"](remaining=0, overage_enabled=False)
    org_id = seed_org_and_entitlement["org_id"]

    app = _build_decorated_app(monkeypatch, db_sessionmaker)
    with TestClient(app) as client:
        resp = client.post(
            "/gated/signal",
            headers={
                "X-Stub-User-Email": "quota@example.com",
                "X-Stub-Org-Id": org_id,
            },
        )
    assert resp.status_code == 402, resp.text
    body = resp.json()
    assert body["detail"]["error"] == "quota_exhausted"
    assert body["detail"]["feature"] == "signals"


@pytest.mark.asyncio
async def test_decorator_sets_response_header(
    db_sessionmaker, seed_org_and_entitlement, monkeypatch
):
    """A successful gated call must set ``X-Entitlement-Remaining``."""
    await seed_org_and_entitlement["insert"](remaining=50, overage_enabled=False)
    org_id = seed_org_and_entitlement["org_id"]

    app = _build_decorated_app(monkeypatch, db_sessionmaker)
    with TestClient(app) as client:
        resp = client.post(
            "/gated/signal",
            headers={
                "X-Stub-User-Email": "ok@example.com",
                "X-Stub-Org-Id": org_id,
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    # One unit consumed → 49 remaining.
    assert resp.headers["X-Entitlement-Remaining"] == "49"


# ---------------------------------------------------------------------------
# GET /api/entitlements shape test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entitlements_shape_matches_spec(
    db_sessionmaker, seed_org_and_entitlement, monkeypatch
):
    """GET /api/entitlements returns the exact JSON shape from §7.4.

    The org is at the ``trader`` tier (seeded by the fixture). We also
    insert a ``backtests`` entitlement row so the feature block is
    fully populated and we can assert on the spec shape.
    """
    await seed_org_and_entitlement["insert"](remaining=442, overage_enabled=True)
    org_id = seed_org_and_entitlement["org_id"]

    # Seed a second row for backtests so the features block has the
    # whole shape populated.
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO entitlements (
                    id, org_id, feature, period_start, period_end,
                    included, remaining, overage_enabled
                )
                VALUES (
                    :id, :org_id, 'backtests', :p_start, :p_end,
                    200, 198, 0
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "p_start": _PERIOD_START,
                "p_end": _PERIOD_END,
            },
        )
        await session.commit()

    # Build a test app that mounts the read router only, with the
    # endpoints session dep overridden to use our SQLite fixture.
    from api.billing import endpoints as endpoints_module
    from api.billing.read import router as read_router

    async def _override_session() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    app = FastAPI()
    app.add_middleware(AuthStubMiddleware)
    app.include_router(read_router, prefix="/api")
    app.dependency_overrides[
        endpoints_module._get_billing_session
    ] = _override_session

    with TestClient(app) as client:
        resp = client.get(
            "/api/entitlements",
            headers={
                "X-Stub-User-Email": "shape@example.com",
                "X-Stub-Org-Id": org_id,
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level keys per §7.4
    assert body["tier"] == "trader"
    assert "period_start" in body
    assert "period_end" in body
    assert "features" in body

    features = body["features"]

    # Consumable features: included + remaining + overage_enabled
    assert features["signals"]["included"] == 442
    assert features["signals"]["remaining"] == 442
    assert features["signals"]["overage_enabled"] is True

    assert features["backtests"]["included"] == 200
    assert features["backtests"]["remaining"] == 198

    # Cap features: {"max": N}
    assert "max" in features["tickers"]
    assert "max" in features["strategy_slots"]

    # Boolean features — real JSON booleans, never 0/1.
    assert features["live_trading"] is True  # trader tier
    assert features["custom_strategies"] is False  # trader tier
    assert isinstance(features["live_trading"], bool)
    assert isinstance(features["custom_strategies"], bool)

    # api_access is the string enum from tiers.yaml, never an int.
    assert features["api_access"] == "read"
    assert isinstance(features["api_access"], str)
