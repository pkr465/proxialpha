"""Tests for the Checkout + Customer Portal endpoints (Task 03).

These tests exercise ``api.billing.endpoints`` against an in-memory
SQLite database that mirrors the relevant subset of Task 01's schema
(``organizations`` and ``subscriptions`` are the only tables touched
by the endpoint code path).

The Stripe SDK is fully mocked — we never make a real API call. Each
test installs a fake ``stripe.checkout.Session.create`` /
``stripe.billing_portal.Session.create`` via monkeypatch and asserts
on the kwargs the endpoint passes.

Auth is provided by :class:`api.middleware.auth_stub.AuthStubMiddleware`
via the ``X-Stub-User-Email`` and ``X-Stub-Org-Id`` headers, which is
the same path the production code will follow once Clerk replaces the
stub in Task 04. The middleware is installed on the test FastAPI
instance so the contract is identical to ``api.server``.

Why SQLite (same rationale as the webhook handler tests):
the sandbox has no Postgres. SQLite gives us real SQL, transactions,
and rapid test isolation. The endpoint queries are written with plain
``WHERE org_id = :org_id`` so they run unmodified under either dialect.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List

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

# Seed env vars BEFORE importing the endpoints module so the price-to-tier
# allow-list is built against known IDs. ``build_price_to_tier_map`` reads
# ``os.environ`` at call time (not at import), so this is safe to do
# inside the test file as long as it runs before the first request.
os.environ.setdefault("STRIPE_PRICE_TRADER_MONTHLY", "price_trader_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_TRADER_ANNUAL", "price_trader_annual_xxx")
os.environ.setdefault("STRIPE_PRICE_PRO_MONTHLY", "price_pro_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_PRO_ANNUAL", "price_pro_annual_xxx")
os.environ.setdefault("STRIPE_PRICE_TEAM_MONTHLY", "price_team_monthly_xxx")
os.environ.setdefault("STRIPE_PRICE_TEAM_ANNUAL", "price_team_annual_xxx")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("APP_URL", "http://localhost:3000")

import stripe  # noqa: E402

from api.billing import endpoints as endpoints_module  # noqa: E402
from api.middleware.auth_stub import AuthStubMiddleware  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal SQLite schema — only tables the endpoint code touches
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
]


# ---------------------------------------------------------------------------
# Stripe SDK fakes
# ---------------------------------------------------------------------------


class _FakeStripeSession:
    """Mimics the small surface of a stripe Session that we read."""

    def __init__(self, url: str, session_id: str):
        self.url = url
        self.id = session_id


class _StripeCallRecorder:
    """Captures calls to ``stripe.*.Session.create`` so tests can assert.

    We use one instance per test (via the ``stripe_calls`` fixture) and
    monkey-patch ``stripe.checkout.Session.create`` and
    ``stripe.billing_portal.Session.create`` to push through this object.
    """

    def __init__(self) -> None:
        self.checkout_calls: List[Dict[str, Any]] = []
        self.portal_calls: List[Dict[str, Any]] = []
        # Override these in tests to make a call raise instead.
        self.checkout_should_fail = False
        self.portal_should_fail = False

    def fake_checkout_create(self, **kwargs: Any) -> _FakeStripeSession:
        if self.checkout_should_fail:
            raise stripe.StripeError("simulated failure")
        self.checkout_calls.append(kwargs)
        return _FakeStripeSession(
            url="https://checkout.stripe.com/c/pay/test_xyz",
            session_id="cs_test_xyz",
        )

    def fake_portal_create(self, **kwargs: Any) -> _FakeStripeSession:
        if self.portal_should_fail:
            raise stripe.StripeError("simulated failure")
        self.portal_calls.append(kwargs)
        return _FakeStripeSession(
            url="https://billing.stripe.com/p/session/test_abc",
            session_id="bps_test_abc",
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def stripe_calls(monkeypatch) -> _StripeCallRecorder:
    """Install a fake stripe SDK that records call kwargs."""
    recorder = _StripeCallRecorder()
    monkeypatch.setattr(
        stripe.checkout.Session, "create", recorder.fake_checkout_create
    )
    monkeypatch.setattr(
        stripe.billing_portal.Session, "create", recorder.fake_portal_create
    )
    return recorder


@pytest.fixture
def app(db_sessionmaker, stripe_calls) -> FastAPI:
    """Build a minimal FastAPI app mounting just the billing endpoints.

    We do NOT import ``api.server`` — that pulls in the trading engine
    (pandas, yfinance) which is overkill for routing tests. Instead we
    construct a fresh FastAPI, install the auth stub middleware, and
    mount only the billing router. The dependency override swaps the
    real DB session for our SQLite fixture session.
    """
    from api.billing import billing_router

    test_app = FastAPI()
    test_app.add_middleware(AuthStubMiddleware)
    test_app.include_router(billing_router, prefix="/api/billing")

    async def _override_session() -> AsyncIterator[AsyncSession]:
        session = db_sessionmaker()
        try:
            yield session
        finally:
            await session.close()

    test_app.dependency_overrides[
        endpoints_module._get_billing_session
    ] = _override_session
    return test_app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest_asyncio.fixture
async def seed_org_no_customer(db_sessionmaker):
    """Insert an organization with no Stripe customer (fresh signup)."""
    org_id = str(uuid.uuid4())
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, stripe_customer_id, tier) "
                "VALUES (:id, :n, NULL, 'free')"
            ),
            {"id": org_id, "n": "Fresh Org"},
        )
        await session.commit()
    return {"org_id": org_id}


@pytest_asyncio.fixture
async def seed_org_with_customer(db_sessionmaker):
    """Insert an organization that already has a Stripe customer ID."""
    org_id = str(uuid.uuid4())
    customer_id = f"cus_test_{uuid.uuid4().hex[:12]}"
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, stripe_customer_id, tier) "
                "VALUES (:id, :n, :c, 'free')"
            ),
            {"id": org_id, "n": "Existing Org", "c": customer_id},
        )
        await session.commit()
    return {"org_id": org_id, "customer_id": customer_id}


@pytest_asyncio.fixture
async def seed_org_with_active_sub(db_sessionmaker):
    """Insert an org + an active subscription so /checkout returns 409."""
    org_id = str(uuid.uuid4())
    customer_id = f"cus_test_{uuid.uuid4().hex[:12]}"
    sub_id = f"sub_test_{uuid.uuid4().hex[:12]}"
    async with db_sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, name, stripe_customer_id, tier) "
                "VALUES (:id, :n, :c, 'trader')"
            ),
            {"id": org_id, "n": "Active Org", "c": customer_id},
        )
        await session.execute(
            text(
                """
                INSERT INTO subscriptions (
                    id, org_id, stripe_subscription_id, stripe_price_id,
                    status, tier, seats,
                    current_period_start, current_period_end
                )
                VALUES (
                    :id, :org_id, :sub_id, :price_id,
                    'active', 'trader', 1,
                    '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z'
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "sub_id": sub_id,
                "price_id": "price_trader_monthly_xxx",
            },
        )
        await session.commit()
    return {"org_id": org_id, "customer_id": customer_id, "sub_id": sub_id}


def _auth_headers(org_id: str, email: str = "test@example.com") -> Dict[str, str]:
    """Build the auth-stub headers a logged-in request would carry."""
    return {
        "X-Stub-User-Email": email,
        "X-Stub-Org-Id": org_id,
    }


# ---------------------------------------------------------------------------
# /checkout tests
# ---------------------------------------------------------------------------


def test_checkout_creates_session_with_valid_price(
    client, seed_org_no_customer, stripe_calls
):
    """A valid price ID + clean org returns 200 with the checkout URL."""
    org_id = seed_org_no_customer["org_id"]
    response = client.post(
        "/api/billing/checkout",
        headers=_auth_headers(org_id),
        json={
            "price_id": "price_trader_monthly_xxx",
            "success_url": "https://app.example.com/billing/success",
            "cancel_url": "https://app.example.com/pricing",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checkout_url"] == "https://checkout.stripe.com/c/pay/test_xyz"

    # Exactly one Stripe call was made and it carried the right shape.
    assert len(stripe_calls.checkout_calls) == 1
    call = stripe_calls.checkout_calls[0]
    assert call["mode"] == "subscription"
    assert call["line_items"] == [
        {"price": "price_trader_monthly_xxx", "quantity": 1}
    ]
    assert call["success_url"] == "https://app.example.com/billing/success"
    assert call["cancel_url"] == "https://app.example.com/pricing"
    # No stripe_customer_id on the org → endpoint must pre-fill email.
    assert call.get("customer_email") == "test@example.com"
    assert "customer" not in call


def test_checkout_rejects_unknown_price_id(client, seed_org_no_customer, stripe_calls):
    """An unknown price ID returns 400 and never calls Stripe."""
    org_id = seed_org_no_customer["org_id"]
    response = client.post(
        "/api/billing/checkout",
        headers=_auth_headers(org_id),
        json={
            "price_id": "price_not_in_allowlist_zzz",
            "success_url": "https://app.example.com/success",
            "cancel_url": "https://app.example.com/cancel",
        },
    )
    assert response.status_code == 400, response.text
    body = response.json()
    # FastAPI wraps HTTPException(detail=...) in {"detail": ...}.
    assert body["detail"]["error"] == "unknown_price_id"
    assert "accepted" in body["detail"]
    # Stripe must NOT have been called for an invalid input.
    assert stripe_calls.checkout_calls == []


def test_checkout_rejects_if_active_subscription_exists(
    client, seed_org_with_active_sub, stripe_calls
):
    """An org with an active subscription gets 409 + a portal URL."""
    org_id = seed_org_with_active_sub["org_id"]
    response = client.post(
        "/api/billing/checkout",
        headers=_auth_headers(org_id),
        json={
            "price_id": "price_pro_monthly_xxx",
            "success_url": "https://app.example.com/success",
            "cancel_url": "https://app.example.com/cancel",
        },
    )
    assert response.status_code == 409, response.text
    body = response.json()
    detail = body["detail"]
    assert "portal_url" in detail
    # Should be the URL our fake portal_session returns.
    assert detail["portal_url"] == "https://billing.stripe.com/p/session/test_abc"

    # The endpoint should have asked Stripe for a portal URL but NOT a
    # checkout session — the whole point of the 409 is to avoid creating
    # a duplicate subscription.
    assert stripe_calls.checkout_calls == []
    assert len(stripe_calls.portal_calls) == 1
    assert (
        stripe_calls.portal_calls[0]["customer"]
        == seed_org_with_active_sub["customer_id"]
    )


def test_checkout_sets_client_reference_id(
    client, seed_org_with_customer, stripe_calls
):
    """``client_reference_id`` MUST equal the org UUID as a string.

    The Task 02 webhook handler reads this back via
    ``checkout.session.completed`` to bind the new Stripe customer to
    the right internal org. If this assertion ever breaks, the entire
    Stripe → us correlation flow breaks with it.
    """
    org_id = seed_org_with_customer["org_id"]
    response = client.post(
        "/api/billing/checkout",
        headers=_auth_headers(org_id),
        json={
            "price_id": "price_pro_annual_xxx",
            "success_url": "https://app.example.com/success",
            "cancel_url": "https://app.example.com/cancel",
        },
    )
    assert response.status_code == 200, response.text
    assert len(stripe_calls.checkout_calls) == 1
    call = stripe_calls.checkout_calls[0]

    # Load-bearing — must be a string equal to the org UUID exactly.
    assert call["client_reference_id"] == org_id
    assert isinstance(call["client_reference_id"], str)
    # And since this org already has a Stripe customer, the call should
    # reuse it rather than asking the user to retype an email.
    assert call["customer"] == seed_org_with_customer["customer_id"]
    assert "customer_email" not in call


# ---------------------------------------------------------------------------
# /portal tests
# ---------------------------------------------------------------------------


def test_portal_returns_404_without_stripe_customer(
    client, seed_org_no_customer, stripe_calls
):
    """An org with no Stripe customer cannot open the portal."""
    org_id = seed_org_no_customer["org_id"]
    response = client.post(
        "/api/billing/portal", headers=_auth_headers(org_id)
    )
    assert response.status_code == 404, response.text
    # We never even called Stripe — that's the point of the 404.
    assert stripe_calls.portal_calls == []


def test_portal_returns_url_when_customer_exists(
    client, seed_org_with_customer, stripe_calls
):
    """An org with a Stripe customer gets back a portal URL."""
    org_id = seed_org_with_customer["org_id"]
    customer_id = seed_org_with_customer["customer_id"]

    response = client.post(
        "/api/billing/portal", headers=_auth_headers(org_id)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["portal_url"] == "https://billing.stripe.com/p/session/test_abc"

    assert len(stripe_calls.portal_calls) == 1
    call = stripe_calls.portal_calls[0]
    assert call["customer"] == customer_id
    # Return URL must be the dashboard, not the pricing page.
    assert call["return_url"].endswith("/dashboard")
