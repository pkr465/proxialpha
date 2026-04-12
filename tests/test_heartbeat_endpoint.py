"""Tests for ``POST /agent/heartbeat`` (Task 06).

These tests exercise :mod:`api.agent.heartbeat` against an in-memory
SQLite database that mirrors the subset of the Task 01 + 0003 schema
that the handler touches (``organizations``, ``subscriptions``,
``entitlements``, ``agents``). Everything runs in-process — no real
Stripe, no real Postgres, no real Clerk.

Why SQLite instead of Postgres
------------------------------

The sandbox has no Postgres. SQLite gives us real SQL, transactions,
and test isolation at rocket speed. All queries in the heartbeat
handler are written with plain ``WHERE ... = :...`` clauses so they
run unmodified under either dialect — the same discipline the Task
02 / 03 tests follow.

JWT strategy
------------

We use the real :mod:`core.jwt_keys` module — NOT a mock. Each test
generates an in-memory RS256 keypair via the dev fallback, issues a
real token through :func:`api.agent.license_issuer.issue_license`,
and POSTs it. That means the verification path is exercised
end-to-end, including signature checking and claim validation.

The server's view of "now" is frozen by setting
``app.state._heartbeat_now`` on the test app so tests have
deterministic timestamps. This is the one and only test hook the
handler reads — production never sets it.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Make the repo importable when pytest is invoked from outside the repo.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Force the dev-generated keypair path in jwt_keys by clearing any
# env-driven key and flipping ENV to dev. MUST happen before the
# first import of jwt_keys in this process — other test files don't
# import it so we're safe.
os.environ.pop("AGENT_SIGNING_KEY_PATH", None)
os.environ.pop("AGENT_SIGNING_KEY_PEM", None)
os.environ["ENV"] = "dev"

from core import jwt_keys  # noqa: E402
from api.agent import heartbeat as heartbeat_module  # noqa: E402
from api.agent.license_issuer import issue_license  # noqa: E402


# ---------------------------------------------------------------------------
# SQLite schema — only tables the handler touches
# ---------------------------------------------------------------------------

# Booleans are stored as INTEGER 0/1 in SQLite. Timestamps are TEXT
# (ISO 8601). The handler's ``_coerce_db_datetime`` accepts both
# ``datetime`` and ISO strings so the same code runs here and in prod.

_SCHEMA_SQL = [
    """
    CREATE TABLE organizations (
        id                 TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        stripe_customer_id TEXT UNIQUE,
        tier               TEXT NOT NULL DEFAULT 'free',
        config_version     INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE subscriptions (
        id                     TEXT PRIMARY KEY,
        org_id                 TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        stripe_subscription_id TEXT UNIQUE NOT NULL,
        stripe_price_id        TEXT NOT NULL,
        status                 TEXT NOT NULL,
        tier                   TEXT NOT NULL,
        seats                  INTEGER NOT NULL DEFAULT 1,
        current_period_start   TEXT NOT NULL,
        current_period_end     TEXT NOT NULL,
        cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,
        metered_item_ids       TEXT NOT NULL DEFAULT '{}',
        created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE agents (
        id                 TEXT PRIMARY KEY,
        org_id             TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        fingerprint        TEXT NOT NULL,
        hostname           TEXT,
        version            TEXT,
        topology           TEXT NOT NULL DEFAULT 'C',
        mode               TEXT NOT NULL DEFAULT 'booting',
        license_jti        TEXT,
        grace_until        TEXT,
        last_heartbeat_at  TEXT,
        last_error         TEXT,
        last_metrics       TEXT NOT NULL DEFAULT '{}',
        config_version     INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXED_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_jwt_keys_cache() -> None:
    """Ensure every test gets a fresh RSA keypair for clean isolation."""
    jwt_keys.reset_cache_for_tests()
    yield
    jwt_keys.reset_cache_for_tests()


@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory SQLite engine per test for full isolation."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
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


@pytest.fixture
def app(db_sessionmaker) -> FastAPI:
    """Build a minimal FastAPI app mounting the agent router.

    We deliberately do NOT import :mod:`api.server` — that pulls in
    the trading engine. Instead we construct a fresh FastAPI,
    override the session dependency to use the SQLite fixture, and
    install the frozen-``now`` hook on ``app.state``.
    """
    from api.agent import agent_router

    test_app = FastAPI()
    test_app.include_router(agent_router, prefix="/agent")
    test_app.state._heartbeat_now = FIXED_NOW

    async def _override_session() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    test_app.dependency_overrides[heartbeat_module._get_agent_session] = (
        _override_session
    )
    return test_app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest_asyncio.fixture
async def seed_active_org(db_sessionmaker):
    """Insert an org + an active trader subscription + some entitlements.

    Returns a dict with ``org_id``, ``agent_id``, ``fingerprint``, the
    pre-seeded entitlement state, and a convenience ``make_token()``
    helper that issues a real RS256 JWT for this org/agent.
    """
    org_id = str(uuid.uuid4())
    sub_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    fingerprint = "fp_" + uuid.uuid4().hex

    period_start = FIXED_NOW - timedelta(days=5)
    period_end = FIXED_NOW + timedelta(days=25)

    async with db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, tier, config_version) "
                "VALUES (:id, :n, 'pro', 0)"
            ),
            {"id": org_id, "n": "Active Org"},
        )
        await session.execute(
            text(
                """
                INSERT INTO subscriptions (
                    id, org_id, stripe_subscription_id, stripe_price_id,
                    status, tier, seats,
                    current_period_start, current_period_end, updated_at
                )
                VALUES (
                    :id, :org_id, :sub_ext, 'price_pro_monthly',
                    'active', 'pro', 1,
                    :ps, :pe, :upd
                )
                """
            ),
            {
                "id": sub_id,
                "org_id": org_id,
                "sub_ext": f"sub_test_{uuid.uuid4().hex[:10]}",
                "ps": period_start.isoformat(),
                "pe": period_end.isoformat(),
                "upd": FIXED_NOW.isoformat(),
            },
        )
        # Entitlements row for the ``signals`` feature.
        await session.execute(
            text(
                """
                INSERT INTO entitlements (
                    id, org_id, feature, period_start, period_end,
                    included, remaining, overage_enabled
                )
                VALUES (
                    :id, :org_id, 'signals', :ps, :pe,
                    10000, 9500, 0
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "ps": period_start.isoformat(),
                "pe": period_end.isoformat(),
            },
        )
        await session.commit()

    def make_token(
        *,
        now: Optional[datetime] = None,
        fp: Optional[str] = None,
        ttl: timedelta = timedelta(hours=24),
        entitlements: Optional[Dict[str, Any]] = None,
        grace_until: Optional[datetime] = None,
    ) -> str:
        return issue_license(
            org_id=org_id,
            agent_id=agent_id,
            agent_fingerprint=fp or fingerprint,
            entitlements_snapshot=entitlements
            or {"tier": "pro", "features": {}, "generated_at": 0},
            grace_until=grace_until,
            now=now or FIXED_NOW,
            ttl=ttl,
        )

    return {
        "org_id": org_id,
        "agent_id": agent_id,
        "fingerprint": fingerprint,
        "sub_id": sub_id,
        "period_end": period_end,
        "make_token": make_token,
    }


def _heartbeat_body(
    *,
    agent_now: datetime = FIXED_NOW,
    version: str = "1.0.3",
    topology: str = "C",
    hostname: str = "trader-box-01",
    metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "agent_id": "ignored-by-handler-uses-jwt-sub",
        "version": version,
        "topology": topology,
        "hostname": hostname,
        "now": agent_now.isoformat(),
        "metrics": metrics or {"uptime_s": 3600, "signals_seen": 42},
    }


async def _read_agent_row(
    db_sessionmaker, org_id: str, agent_id: str
) -> Optional[Dict[str, Any]]:
    async with db_sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT id, fingerprint, mode, version, hostname, "
                "last_heartbeat_at, last_metrics, grace_until, config_version "
                "FROM agents WHERE org_id = :org AND id = :id"
            ),
            {"org": org_id, "id": agent_id},
        )
        row = result.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "fingerprint": row[1],
            "mode": row[2],
            "version": row[3],
            "hostname": row[4],
            "last_heartbeat_at": row[5],
            "last_metrics": row[6],
            "grace_until": row[7],
            "config_version": row[8],
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_heartbeat_valid_token_returns_refreshed(client, seed_active_org):
    """Happy path: valid token → 200 with a fresh license."""
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rotate_token"] is True
    assert isinstance(body["license"], str) and body["license"] != token
    # The refreshed token must verify.
    refreshed_claims = jwt_keys.verify(body["license"], now=FIXED_NOW)
    assert refreshed_claims["org_id"] == seed_active_org["org_id"]
    assert refreshed_claims["sub"] == seed_active_org["agent_id"]
    assert refreshed_claims["agent_fingerprint"] == seed_active_org["fingerprint"]


def test_heartbeat_expired_token_returns_401(client, seed_active_org):
    """Expired license → 401 invalid_token/expired."""
    # Issue a token that expired an hour ago.
    token = seed_active_org["make_token"](
        now=FIXED_NOW - timedelta(hours=25),
        ttl=timedelta(hours=24),
    )
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_token"
    assert detail["reason"] == "expired"


def test_heartbeat_bad_signature_returns_401(client, seed_active_org):
    """Tampered signature → 401 invalid_token/signature."""
    token = seed_active_org["make_token"]()
    # Flip one character in the signature segment.
    header, payload, signature = token.split(".")
    tampered_sig = signature[:-2] + ("aa" if signature[-2:] != "aa" else "bb")
    tampered = f"{header}.{payload}.{tampered_sig}"
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_token"
    # Depending on PyJWT's base64 parser the tampered sig is
    # classified as "signature" or "malformed" — both are fine.
    assert detail["reason"] in ("signature", "malformed")


@pytest.mark.asyncio
async def test_heartbeat_canceled_subscription_returns_403(
    client, seed_active_org, db_sessionmaker
):
    """Org with canceled sub → 403 license_revoked."""
    async with db_sessionmaker() as session:
        await session.execute(
            text("UPDATE subscriptions SET status = 'canceled' WHERE org_id = :o"),
            {"o": seed_active_org["org_id"]},
        )
        await session.commit()
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "license_revoked"


@pytest.mark.asyncio
async def test_heartbeat_past_due_returns_402_with_grace_info(
    client, seed_active_org, db_sessionmaker
):
    """Past-due sub → 402 past_due with grace_ends_at set."""
    async with db_sessionmaker() as session:
        await session.execute(
            text("UPDATE subscriptions SET status = 'past_due' WHERE org_id = :o"),
            {"o": seed_active_org["org_id"]},
        )
        await session.commit()
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 402
    detail = resp.json()["detail"]
    assert detail["error"] == "past_due"
    assert detail["grace_ends_at"] is not None
    # The body must carry a parseable ISO timestamp.
    parsed = datetime.fromisoformat(detail["grace_ends_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_heartbeat_clock_skew_5_minutes_accepted(client, seed_active_org):
    """|agent.now - server.now| = 300s → still accepted (inclusive)."""
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(agent_now=FIXED_NOW + timedelta(seconds=300)),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


def test_heartbeat_clock_skew_6_minutes_rejected(client, seed_active_org):
    """|agent.now - server.now| = 360s → 401 clock_skew."""
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(agent_now=FIXED_NOW + timedelta(seconds=360)),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "clock_skew"


@pytest.mark.asyncio
async def test_heartbeat_first_heartbeat_stores_fingerprint(
    client, seed_active_org, db_sessionmaker
):
    """First heartbeat creates the agents row with the JWT's fingerprint."""
    # Sanity: no row yet.
    before = await _read_agent_row(
        db_sessionmaker, seed_active_org["org_id"], seed_active_org["agent_id"]
    )
    assert before is None

    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    after = await _read_agent_row(
        db_sessionmaker, seed_active_org["org_id"], seed_active_org["agent_id"]
    )
    assert after is not None
    assert after["fingerprint"] == seed_active_org["fingerprint"]
    assert after["mode"] == "running"
    assert after["version"] == "1.0.3"
    assert after["hostname"] == "trader-box-01"
    # last_metrics is stored as JSON text; assert the contents round-trip.
    assert json.loads(after["last_metrics"])["signals_seen"] == 42


@pytest.mark.asyncio
async def test_heartbeat_fingerprint_mismatch_returns_409(
    client, seed_active_org, db_sessionmaker
):
    """Stored fingerprint != JWT fingerprint → 409 fingerprint_mismatch."""
    # Pre-seed the row with a DIFFERENT fingerprint.
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO agents (
                    id, org_id, fingerprint, mode, topology,
                    last_heartbeat_at, last_metrics, config_version
                )
                VALUES (
                    :id, :org, :fp, 'running', 'C',
                    :now, '{}', 0
                )
                """
            ),
            {
                "id": seed_active_org["agent_id"],
                "org": seed_active_org["org_id"],
                "fp": "fp_stored_original",
                "now": (FIXED_NOW - timedelta(hours=1)).isoformat(),
            },
        )
        await session.commit()

    # The JWT carries the OTHER fingerprint — the one seed_active_org
    # generated. That's the mismatch.
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "fingerprint_mismatch"


@pytest.mark.asyncio
async def test_heartbeat_returns_config_bundle_when_version_changed(
    client, seed_active_org, db_sessionmaker
):
    """organizations.config_version > agent's → response contains config_bundle."""
    # Bump the org config version so the handler sees a delta.
    async with db_sessionmaker() as session:
        await session.execute(
            text("UPDATE organizations SET config_version = 5 WHERE id = :id"),
            {"id": seed_active_org["org_id"]},
        )
        await session.commit()

    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_bundle"] is not None
    assert body["config_bundle"]["version"] == 5

    # The agents row should now have config_version = 5 so the next
    # heartbeat gets config_bundle=None.
    after = await _read_agent_row(
        db_sessionmaker, seed_active_org["org_id"], seed_active_org["agent_id"]
    )
    assert after["config_version"] == 5

    resp2 = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["config_bundle"] is None


def test_heartbeat_refreshed_token_has_updated_entitlements_snapshot(
    client, seed_active_org
):
    """The fresh license carries the current entitlements snapshot."""
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    refreshed = resp.json()["license"]
    claims = jwt_keys.verify(refreshed, now=FIXED_NOW)
    snap = claims["entitlements_snapshot"]
    assert snap["tier"] == "pro"
    assert "signals" in snap["features"]
    assert snap["features"]["signals"]["included"] == 10000
    assert snap["features"]["signals"]["remaining"] == 9500
    assert snap["features"]["signals"]["overage_enabled"] is False
    assert snap["generated_at"] == int(FIXED_NOW.timestamp())


@pytest.mark.asyncio
async def test_heartbeat_grace_until_slides_forward_after_contact(
    client, seed_active_org, db_sessionmaker
):
    """Stale grace_until (> 6 days old) refreshes to now + 7 days."""
    stale_grace = FIXED_NOW - timedelta(days=10)  # well past the 6-day threshold
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO agents (
                    id, org_id, fingerprint, mode, topology,
                    grace_until, last_heartbeat_at, last_metrics, config_version
                )
                VALUES (
                    :id, :org, :fp, 'offline_grace', 'C',
                    :gu, :hb, '{}', 0
                )
                """
            ),
            {
                "id": seed_active_org["agent_id"],
                "org": seed_active_org["org_id"],
                "fp": seed_active_org["fingerprint"],
                "gu": stale_grace.isoformat(),
                "hb": (FIXED_NOW - timedelta(days=10)).isoformat(),
            },
        )
        await session.commit()

    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    # Agents row grace_until should now be FIXED_NOW + 7d.
    after = await _read_agent_row(
        db_sessionmaker, seed_active_org["org_id"], seed_active_org["agent_id"]
    )
    assert after["grace_until"] is not None
    new_grace = datetime.fromisoformat(after["grace_until"].replace("Z", "+00:00"))
    expected = FIXED_NOW + timedelta(days=7)
    assert abs((new_grace - expected).total_seconds()) < 2

    # And the refreshed license should carry the same grace_until.
    claims = jwt_keys.verify(resp.json()["license"], now=FIXED_NOW)
    assert claims["grace_until"] == int(expected.timestamp())


@pytest.mark.asyncio
async def test_heartbeat_updates_agents_last_heartbeat_at(
    client, seed_active_org, db_sessionmaker
):
    """Each successful heartbeat writes last_heartbeat_at to the server's now."""
    token = seed_active_org["make_token"]()
    resp = client.post(
        "/agent/heartbeat",
        json=_heartbeat_body(),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text

    after = await _read_agent_row(
        db_sessionmaker, seed_active_org["org_id"], seed_active_org["agent_id"]
    )
    assert after is not None
    stored = datetime.fromisoformat(after["last_heartbeat_at"].replace("Z", "+00:00"))
    assert abs((stored - FIXED_NOW).total_seconds()) < 2
