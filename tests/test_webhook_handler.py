"""Tests for the Stripe webhook handler (Task 02).

These tests exercise ``api.billing.webhook`` and ``api.billing.handlers``
against an in-memory SQLite database that mirrors the Postgres schema
from Task 01's migration.

Why SQLite
----------

The sandbox this project is developed in does not have Postgres
installed. SQLite gives us real SQL, transactions, ``ON CONFLICT``, and
schema validation with zero infrastructure. The handler code is
intentionally written with cross-dialect SQL so the same statements
pass under both engines.

What we give up
~~~~~~~~~~~~~~~

*   Row Level Security. The webhook handler already runs with
    bypass-RLS privileges in production, so losing RLS in tests doesn't
    weaken coverage.
*   ``billing_raw`` schema. SQLite has no multi-schema concept, so we
    monkey-patch ``api.billing.webhook.STRIPE_EVENTS_TABLE`` to the
    bare table name ``stripe_events`` for the duration of the test run.
*   ``jsonb``. We store the payload as TEXT. The handlers don't query
    inside it.

Covered tests (7, per Task 02 prompt):

1. ``test_signature_verification_rejects_invalid``
2. ``test_checkout_completed_seeds_entitlements``
3. ``test_duplicate_event_is_idempotent``
4. ``test_subscription_updated_reseeds_on_tier_change``
5. ``test_subscription_deleted_downgrades_to_free``
6. ``test_invoice_payment_failed_keeps_entitlements``
7. ``test_webhook_returns_200_under_3_seconds``
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict

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

# Make the repo importable if pytest is run from outside the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Seed env vars so build_price_to_tier_map resolves to known IDs BEFORE
# we import the handlers module (so no stale mapping is cached).
os.environ.setdefault("STRIPE_PRICE_TRADER_MONTHLY", "price_trader_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_TRADER_ANNUAL", "price_trader_annual_xxx")
os.environ.setdefault("STRIPE_PRICE_PRO_MONTHLY", "price_pro_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_PRO_ANNUAL", "price_pro_annual_xxx")
os.environ.setdefault("STRIPE_PRICE_TEAM_MONTHLY", "price_team_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_TEAM_ANNUAL", "price_team_annual_xxx")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_test_secret_abcdef")

from api.billing import webhook as webhook_module
from api.billing import handlers as handlers_module
from api.billing.entitlement_seeder import seed_entitlements  # noqa: F401
from core.settings import get_settings


# ---------------------------------------------------------------------------
# SQLite schema — mirrors Task 01 migration
# ---------------------------------------------------------------------------

# SQLite is permissive about CHECK constraints and UUID types; we store
# UUIDs as TEXT and let Python handle conversion. The handler code uses
# ``str(uuid.uuid4())`` for inserts, which is portable.
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
    # SQLite has no schemas; use a flat table and patch the prefix
    # in the webhook module.
    """
    CREATE TABLE stripe_events (
        id               TEXT PRIMARY KEY,
        event_type       TEXT NOT NULL,
        received_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        processed_at     TEXT,
        payload          TEXT NOT NULL,
        processing_error TEXT
    )
    """,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    """Fresh in-memory SQLite engine per test — full isolation."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
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


@pytest.fixture
def patched_schema_prefix(monkeypatch):
    """Rewrite the stripe_events table name for SQLite tests.

    The production handler references ``billing_raw.stripe_events``
    (Postgres schema qualification). SQLite has no schemas, so the
    conftest redirects to the flat table name.
    """
    monkeypatch.setattr(
        webhook_module, "STRIPE_EVENTS_TABLE", "stripe_events"
    )


@pytest.fixture
def app(db_sessionmaker, patched_schema_prefix) -> FastAPI:
    """Build a minimal FastAPI app that mounts just the billing router.

    We intentionally do NOT import ``api.server`` here — that module
    pulls in the full trading engine (strategies, backtesting, pandas)
    which is overkill for a webhook unit test. The billing router can
    be tested in isolation by mounting it on a fresh FastAPI.
    """
    from api.billing import billing_router

    app = FastAPI()
    app.include_router(billing_router, prefix="/api/billing")

    async def _override_session() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    app.dependency_overrides[webhook_module._get_webhook_session] = (
        _override_session
    )
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest_asyncio.fixture
async def seed_org(db_sessionmaker):
    """Insert an organization + link it to a Stripe customer.

    Returns a dict with ``org_id`` and ``customer_id`` for the test to
    use in event payloads. The Stripe customer ID is pre-linked so
    subscription.* events can find the org without a prior checkout.
    """
    org_id = str(uuid.uuid4())
    customer_id = f"cus_test_{uuid.uuid4().hex[:12]}"
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, stripe_customer_id, tier) "
                "VALUES (:id, :n, :c, 'free')"
            ),
            {"id": org_id, "n": "Test Org", "c": customer_id},
        )
        await session.commit()
    return {"org_id": org_id, "customer_id": customer_id}


# ---------------------------------------------------------------------------
# Event payload builders
# ---------------------------------------------------------------------------


def _now_ts() -> int:
    return int(time.time())


def _period_bounds() -> tuple[int, int]:
    start = _now_ts()
    end = start + 30 * 24 * 3600
    return start, end


def _checkout_event(
    *,
    org_id: str,
    customer_id: str,
    price_id: str,
    subscription_id: str,
    event_id: str = None,
) -> Dict[str, Any]:
    period_start, period_end = _period_bounds()
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_test_{uuid.uuid4().hex[:16]}",
                "object": "checkout.session",
                "mode": "subscription",
                "client_reference_id": org_id,
                "customer": customer_id,
                "subscription": {
                    "id": subscription_id,
                    "object": "subscription",
                    "customer": customer_id,
                    "status": "active",
                    "quantity": 1,
                    "cancel_at_period_end": False,
                    "current_period_start": period_start,
                    "current_period_end": period_end,
                    "items": {
                        "data": [
                            {
                                "id": f"si_{uuid.uuid4().hex[:12]}",
                                "price": {
                                    "id": price_id,
                                    "recurring": {"usage_type": "licensed"},
                                },
                            }
                        ]
                    },
                },
            }
        },
    }


def _subscription_updated_event(
    *,
    customer_id: str,
    price_id: str,
    subscription_id: str,
    status: str = "active",
    event_id: str = None,
) -> Dict[str, Any]:
    period_start, period_end = _period_bounds()
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": subscription_id,
                "object": "subscription",
                "customer": customer_id,
                "status": status,
                "quantity": 1,
                "cancel_at_period_end": False,
                "current_period_start": period_start,
                "current_period_end": period_end,
                "items": {
                    "data": [
                        {
                            "id": f"si_{uuid.uuid4().hex[:12]}",
                            "price": {
                                "id": price_id,
                                "recurring": {"usage_type": "licensed"},
                            },
                        }
                    ]
                },
            }
        },
    }


def _subscription_deleted_event(
    *, customer_id: str, subscription_id: str
) -> Dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": subscription_id,
                "object": "subscription",
                "customer": customer_id,
                "status": "canceled",
            }
        },
    }


def _invoice_payment_failed_event(
    *, customer_id: str, subscription_id: str
) -> Dict[str, Any]:
    return {
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "object": "event",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": f"in_{uuid.uuid4().hex[:16]}",
                "object": "invoice",
                "customer": customer_id,
                "subscription": subscription_id,
            }
        },
    }


# ---------------------------------------------------------------------------
# Signing helper — replicates stripe.Webhook.construct_event input format
# ---------------------------------------------------------------------------

_WEBHOOK_SECRET = "whsec_test_secret_abcdef"


def _sign_stripe_payload(payload: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    """Produce a ``Stripe-Signature`` header value for ``payload``.

    Matches ``stripe.WebhookSignature.verify_header`` format:
    ``t=<unix>,v1=<hmac_sha256_hex>``
    where the HMAC is computed over ``<t>.<payload_string>``.
    """
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    sig = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={sig}"


def _post_event(
    client: TestClient, event: Dict[str, Any], *, valid_signature: bool = True
):
    body = json.dumps(event).encode("utf-8")
    if valid_signature:
        sig = _sign_stripe_payload(body)
    else:
        sig = "t=1,v1=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    return client.post(
        "/api/billing/webhook",
        content=body,
        headers={
            "Stripe-Signature": sig,
            "Content-Type": "application/json",
        },
    )


# ---------------------------------------------------------------------------
# Settings patch — make sure get_settings returns our test webhook secret
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_webhook_secret(monkeypatch):
    """Ensure the settings singleton returns our known test secret."""
    get_settings.cache_clear()
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Async DB assertion helpers
# ---------------------------------------------------------------------------


async def _fetch_one(db_sessionmaker, sql: str, params: Dict[str, Any]):
    async with db_sessionmaker() as session:
        res = await session.execute(text(sql), params)
        return res.fetchone()


async def _fetch_all(db_sessionmaker, sql: str, params: Dict[str, Any]):
    async with db_sessionmaker() as session:
        res = await session.execute(text(sql), params)
        return res.fetchall()


# ===========================================================================
# 1. Signature verification
# ===========================================================================


def test_signature_verification_rejects_invalid(client, db_sessionmaker, seed_org):
    """Bad signature → 400, no DB writes."""
    import asyncio

    event = _subscription_deleted_event(
        customer_id=seed_org["customer_id"],
        subscription_id="sub_test_xxx",
    )
    resp = _post_event(client, event, valid_signature=False)
    assert resp.status_code == 400, resp.text

    # stripe_events must be empty
    rows = asyncio.get_event_loop().run_until_complete(
        _fetch_all(db_sessionmaker, "SELECT id FROM stripe_events", {})
    )
    assert rows == []


# ===========================================================================
# 2. checkout.session.completed → seeds entitlements
# ===========================================================================


def test_checkout_completed_seeds_entitlements(client, db_sessionmaker, seed_org):
    """Fire checkout event, assert Trader-tier entitlements show up."""
    import asyncio

    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"
    event = _checkout_event(
        org_id=seed_org["org_id"],
        customer_id=seed_org["customer_id"],
        price_id=os.environ["STRIPE_PRICE_TRADER_MONTHLY"],
        subscription_id=sub_id,
    )
    resp = _post_event(client, event)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}

    # Expect one stripe_events row, marked processed.
    ev_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT id, event_type, processed_at FROM stripe_events WHERE id = :id",
            {"id": event["id"]},
        )
    )
    assert ev_row is not None
    assert ev_row[1] == "checkout.session.completed"
    assert ev_row[2] is not None  # processed

    # Expect a subscriptions row with tier=trader.
    sub_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT tier, status FROM subscriptions WHERE stripe_subscription_id = :s",
            {"s": sub_id},
        )
    )
    assert sub_row is not None
    assert sub_row[0] == "trader"
    assert sub_row[1] == "active"

    # Expect entitlement rows per numeric feature with Trader quotas.
    ent_rows = asyncio.get_event_loop().run_until_complete(
        _fetch_all(
            db_sessionmaker,
            "SELECT feature, included, remaining FROM entitlements "
            "WHERE org_id = :o ORDER BY feature",
            {"o": seed_org["org_id"]},
        )
    )
    as_dict = {r[0]: (r[1], r[2]) for r in ent_rows}
    assert as_dict["signals"] == (500, 500), as_dict
    assert as_dict["backtests"] == (200, 200), as_dict
    assert as_dict["tickers"] == (25, 25), as_dict
    assert as_dict["strategy_slots"] == (5, 5), as_dict

    # And the org's effective tier.
    org_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT tier FROM organizations WHERE id = :o",
            {"o": seed_org["org_id"]},
        )
    )
    assert org_row[0] == "trader"


# ===========================================================================
# 3. Duplicate event idempotency
# ===========================================================================


def test_duplicate_event_is_idempotent(client, db_sessionmaker, seed_org):
    """Same event fired twice → exactly one stripe_events row, one sub row."""
    import asyncio

    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"
    event = _checkout_event(
        org_id=seed_org["org_id"],
        customer_id=seed_org["customer_id"],
        price_id=os.environ["STRIPE_PRICE_PRO_MONTHLY"],
        subscription_id=sub_id,
    )

    r1 = _post_event(client, event)
    assert r1.status_code == 200
    assert r1.json() == {"status": "ok"}

    r2 = _post_event(client, event)
    assert r2.status_code == 200
    assert r2.json() == {"status": "replay"}

    # Exactly one row in stripe_events.
    ev_rows = asyncio.get_event_loop().run_until_complete(
        _fetch_all(
            db_sessionmaker,
            "SELECT id FROM stripe_events WHERE id = :id",
            {"id": event["id"]},
        )
    )
    assert len(ev_rows) == 1

    # Exactly one row in subscriptions for this sub_id.
    sub_rows = asyncio.get_event_loop().run_until_complete(
        _fetch_all(
            db_sessionmaker,
            "SELECT id FROM subscriptions WHERE stripe_subscription_id = :s",
            {"s": sub_id},
        )
    )
    assert len(sub_rows) == 1


# ===========================================================================
# 4. subscription.updated → tier change triggers reseed
# ===========================================================================


def test_subscription_updated_reseeds_on_tier_change(
    client, db_sessionmaker, seed_org
):
    """Trader → Pro: signals entitlement goes 500 → 5000."""
    import asyncio

    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"

    # Start with Trader.
    ev1 = _checkout_event(
        org_id=seed_org["org_id"],
        customer_id=seed_org["customer_id"],
        price_id=os.environ["STRIPE_PRICE_TRADER_MONTHLY"],
        subscription_id=sub_id,
    )
    r1 = _post_event(client, ev1)
    assert r1.status_code == 200

    # Confirm Trader quota.
    before = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT included FROM entitlements "
            "WHERE org_id = :o AND feature = 'signals'",
            {"o": seed_org["org_id"]},
        )
    )
    assert before[0] == 500

    # Upgrade to Pro.
    ev2 = _subscription_updated_event(
        customer_id=seed_org["customer_id"],
        price_id=os.environ["STRIPE_PRICE_PRO_MONTHLY"],
        subscription_id=sub_id,
    )
    r2 = _post_event(client, ev2)
    assert r2.status_code == 200, r2.text

    # The subscription.updated handler reseeds against its own period
    # bounds (computed fresh). The old (trader) row has period_start
    # from the checkout event and remains — we assert that a Pro row
    # now exists with included=5000 for *some* period_start on this org.
    latest = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT included FROM entitlements "
            "WHERE org_id = :o AND feature = 'signals' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"o": seed_org["org_id"]},
        )
    )
    assert latest[0] == 5000, f"expected 5000 after Pro upgrade, got {latest[0]}"

    # And the subscription row reflects Pro tier.
    sub_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT tier FROM subscriptions WHERE stripe_subscription_id = :s",
            {"s": sub_id},
        )
    )
    assert sub_row[0] == "pro"

    # And the org's effective tier.
    org_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT tier FROM organizations WHERE id = :o",
            {"o": seed_org["org_id"]},
        )
    )
    assert org_row[0] == "pro"


# ===========================================================================
# 5. subscription.deleted → downgrade to Free
# ===========================================================================


def test_subscription_deleted_downgrades_to_free(
    client, db_sessionmaker, seed_org
):
    """subscription.deleted: org → free, signals quota → 20."""
    import asyncio

    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"

    # Start on Pro.
    _post_event(
        client,
        _checkout_event(
            org_id=seed_org["org_id"],
            customer_id=seed_org["customer_id"],
            price_id=os.environ["STRIPE_PRICE_PRO_MONTHLY"],
            subscription_id=sub_id,
        ),
    )

    # Cancel.
    resp = _post_event(
        client,
        _subscription_deleted_event(
            customer_id=seed_org["customer_id"],
            subscription_id=sub_id,
        ),
    )
    assert resp.status_code == 200, resp.text

    # Subscription row marked canceled.
    sub_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT status FROM subscriptions WHERE stripe_subscription_id = :s",
            {"s": sub_id},
        )
    )
    assert sub_row[0] == "canceled"

    # Entitlements reseeded with Free values. Latest signals row must be 20.
    latest = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT included FROM entitlements "
            "WHERE org_id = :o AND feature = 'signals' "
            "ORDER BY updated_at DESC LIMIT 1",
            {"o": seed_org["org_id"]},
        )
    )
    assert latest[0] == 20

    org_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT tier FROM organizations WHERE id = :o",
            {"o": seed_org["org_id"]},
        )
    )
    assert org_row[0] == "free"


# ===========================================================================
# 6. invoice.payment_failed → status=past_due, entitlements untouched
# ===========================================================================


def test_invoice_payment_failed_keeps_entitlements(
    client, db_sessionmaker, seed_org
):
    """payment_failed sets past_due but does not mutate entitlements."""
    import asyncio

    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"

    # Seed Trader state.
    _post_event(
        client,
        _checkout_event(
            org_id=seed_org["org_id"],
            customer_id=seed_org["customer_id"],
            price_id=os.environ["STRIPE_PRICE_TRADER_MONTHLY"],
            subscription_id=sub_id,
        ),
    )

    # Capture pre-failure entitlements.
    pre = asyncio.get_event_loop().run_until_complete(
        _fetch_all(
            db_sessionmaker,
            "SELECT feature, included, remaining FROM entitlements "
            "WHERE org_id = :o ORDER BY feature",
            {"o": seed_org["org_id"]},
        )
    )

    # Simulate the invoice failing.
    resp = _post_event(
        client,
        _invoice_payment_failed_event(
            customer_id=seed_org["customer_id"],
            subscription_id=sub_id,
        ),
    )
    assert resp.status_code == 200, resp.text

    # Subscription is past_due.
    sub_row = asyncio.get_event_loop().run_until_complete(
        _fetch_one(
            db_sessionmaker,
            "SELECT status FROM subscriptions WHERE stripe_subscription_id = :s",
            {"s": sub_id},
        )
    )
    assert sub_row[0] == "past_due"

    # Entitlements unchanged.
    post = asyncio.get_event_loop().run_until_complete(
        _fetch_all(
            db_sessionmaker,
            "SELECT feature, included, remaining FROM entitlements "
            "WHERE org_id = :o ORDER BY feature",
            {"o": seed_org["org_id"]},
        )
    )
    assert list(pre) == list(post), (pre, post)


# ===========================================================================
# 7. Latency — success path returns 200 in well under 3 seconds
# ===========================================================================


def test_webhook_returns_200_under_3_seconds(client, db_sessionmaker, seed_org):
    """The full happy path must complete comfortably inside Stripe's 3s budget."""
    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"
    event = _checkout_event(
        org_id=seed_org["org_id"],
        customer_id=seed_org["customer_id"],
        price_id=os.environ["STRIPE_PRICE_TRADER_MONTHLY"],
        subscription_id=sub_id,
    )

    start = time.perf_counter()
    resp = _post_event(client, event)
    elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    assert elapsed < 3.0, f"webhook took {elapsed:.3f}s, budget is 3s"
