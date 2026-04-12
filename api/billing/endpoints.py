"""FastAPI router for Stripe Checkout + Customer Portal endpoints.

Mounted at ``/api/billing`` alongside :mod:`api.billing.webhook` (see
:mod:`api.billing.__init__`). Two routes:

*   ``POST /checkout`` — create a Stripe Checkout Session for a tier
    upgrade. The frontend redirects the user to the returned URL.
*   ``POST /portal`` — create a Stripe Customer Portal session so the
    user can manage their existing subscription.

Phase 1 PRD §7.1 / §7.2.

Auth
----

Both endpoints require an authed user with an org context. In Phase 1
this comes from :class:`api.middleware.auth_stub.AuthStubMiddleware`,
which reads ``X-Stub-User-Email`` and ``X-Stub-Org-Id`` headers and
puts them on ``request.state``. The handlers call
:func:`api.middleware.auth_stub.require_authed_org` which returns
``(StubUser, uuid.UUID)`` or raises 401.

Task 04 will swap the stub for a real Clerk JWT verifier; this file
does not need to change because it only reads the typed
``request.state.user`` / ``request.state.org_id`` contract.

Load-bearing details
--------------------

*   ``client_reference_id=str(org_id)`` is set on every Checkout
    session. The webhook handler in :mod:`api.billing.handlers` reads
    this back via ``checkout.session.completed`` and uses it to bind
    the new Stripe customer to our internal org. **Do not skip.**

*   ``price_id`` is validated against the env-mapped allow-list built
    by :func:`api.billing.entitlement_seeder.build_price_to_tier_map`.
    Anything not in that map gets a 400 with the list of accepted
    prices — we never just forward arbitrary user input to Stripe.

*   If the org already has an ``active``, ``trialing``, or
    ``past_due`` subscription, ``/checkout`` returns 409 with a
    Customer Portal URL instead of creating a duplicate sub. The
    frontend redirects to that URL so the user changes plan in the
    portal.

Session acquisition
-------------------

We expose :func:`_get_billing_session` as a FastAPI dependency that
yields an :class:`AsyncSession` bound to a fresh DB connection. In
production it builds an engine from settings; in tests it is
overridden via ``app.dependency_overrides`` to point at a SQLite
fixture. The endpoints query ``organizations`` and ``subscriptions``
directly with ``WHERE org_id = :org_id`` (so the queries are correct
even before RLS is enforced) and never set ``app.current_org_id``
themselves — Phase 1's RLS is enabled via the bg-worker role's
BYPASSRLS bit, mirroring the webhook handler.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.billing.entitlement_seeder import build_price_to_tier_map
from api.billing.schemas import (
    ActiveSubscriptionError,
    CheckoutRequest,
    CheckoutResponse,
    PortalResponse,
)
from api.middleware.auth_stub import require_authed_org
from core.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Engine / session for endpoint context
# ---------------------------------------------------------------------------

# Separate engine singleton from the webhook handler so the two can be
# overridden independently in tests. Both ultimately point at the same
# DB in production.
_endpoint_engine = None
_endpoint_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_or_create_endpoint_engine():
    """Lazily build the endpoint-context engine from settings."""
    global _endpoint_engine, _endpoint_sessionmaker
    if _endpoint_engine is None:
        settings = get_settings()
        _endpoint_engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, pool_size=5
        )
        _endpoint_sessionmaker = async_sessionmaker(
            bind=_endpoint_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _endpoint_sessionmaker


async def _get_billing_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a billing-endpoint session.

    Tests override this dependency at the app level to swap in a
    SQLite-backed fixture session. Production uses the lazily-built
    async engine pointing at the main control-plane DB.
    """
    maker = _get_or_create_endpoint_engine()
    assert maker is not None
    session = maker()
    try:
        yield session
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Stripe configuration
# ---------------------------------------------------------------------------


def _configure_stripe() -> None:
    """Set ``stripe.api_key`` from settings on every request.

    Cheap (string assignment) and idempotent. Doing it per-request
    rather than at import time means tests can monkey-patch
    ``get_settings`` and have the change take effect without a module
    reload.
    """
    stripe.api_key = get_settings().stripe_secret_key


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


# Statuses that count as "the org already has a subscription". Anything
# else (canceled, incomplete, incomplete_expired) is treated as "no
# active sub" — the user can start a new checkout flow.
_BLOCKING_SUB_STATUSES = ("active", "trialing", "past_due")


async def _fetch_org(
    session: AsyncSession, org_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """Return ``{id, name, stripe_customer_id}`` or None if missing."""
    result = await session.execute(
        text(
            "SELECT id, name, stripe_customer_id "
            "FROM organizations WHERE id = :id"
        ),
        {"id": str(org_id)},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "stripe_customer_id": row[2],
    }


async def _has_blocking_subscription(
    session: AsyncSession, org_id: uuid.UUID
) -> bool:
    """True if the org has an active/trialing/past_due subscription."""
    placeholders = ", ".join(f":s{i}" for i in range(len(_BLOCKING_SUB_STATUSES)))
    params: Dict[str, Any] = {"org_id": str(org_id)}
    for i, status in enumerate(_BLOCKING_SUB_STATUSES):
        params[f"s{i}"] = status
    result = await session.execute(
        text(
            f"SELECT 1 FROM subscriptions "
            f"WHERE org_id = :org_id AND status IN ({placeholders}) "
            f"LIMIT 1"
        ),
        params,
    )
    return result.fetchone() is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout_session(
    request: Request,
    body: CheckoutRequest,
    session: AsyncSession = Depends(_get_billing_session),
) -> Any:
    """Create a Stripe Checkout Session for a tier upgrade.

    Returns ``{checkout_url}`` on success. The frontend should
    immediately redirect the user to that URL.

    Errors:

    * 401 — caller is not authenticated (no org context).
    * 400 — ``price_id`` is not in the configured allow-list.
    * 404 — the org row could not be found (auth header is stale).
    * 409 — the org already has an active/trialing/past_due
      subscription. Body includes a ``portal_url`` to redirect to.
    * 502 — Stripe API call failed (we surface this so the frontend
      can show a "try again" rather than treating it as a 4xx).
    """
    user, org_id = require_authed_org(request)
    _configure_stripe()

    # Validate the requested price against tiers.yaml + env vars.
    price_to_tier = build_price_to_tier_map()
    if body.price_id not in price_to_tier:
        log.warning(
            "checkout: rejected unknown price_id=%s for org=%s",
            body.price_id,
            org_id,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_price_id",
                "message": "price_id is not in the configured allow-list",
                "accepted": sorted(price_to_tier.keys()),
            },
        )

    # Fetch the org so we can decide between ``customer=`` and
    # ``customer_email=`` and surface 404 if the auth header is stale.
    org = await _fetch_org(session, org_id)
    if org is None:
        log.warning("checkout: org_id=%s from auth header has no DB row", org_id)
        raise HTTPException(status_code=404, detail="Organization not found")

    # Block duplicate-checkout flows: if a sub is already in flight,
    # send the user to the portal instead. We compute the portal URL
    # eagerly *only when* there's a blocking sub so the happy path
    # doesn't pay for a wasted Stripe call.
    if await _has_blocking_subscription(session, org_id):
        portal_url: Optional[str] = None
        stripe_customer_id = org.get("stripe_customer_id")
        if stripe_customer_id:
            try:
                portal_session = stripe.billing_portal.Session.create(
                    customer=stripe_customer_id,
                    return_url=get_settings().app_url + "/dashboard",
                )
                portal_url = portal_session.url
            except Exception as exc:  # pragma: no cover - defensive
                log.exception(
                    "checkout: failed to build portal redirect url for org=%s: %s",
                    org_id,
                    exc,
                )
        body_dict = ActiveSubscriptionError(portal_url=portal_url).model_dump()
        log.info(
            "checkout: blocked org=%s — already has active subscription",
            org_id,
        )
        raise HTTPException(status_code=409, detail=body_dict)

    # Build the Stripe Checkout payload. ``client_reference_id`` is
    # LOAD-BEARING — the webhook handler reads it back to bind the new
    # Stripe customer to this org. Do not remove.
    checkout_kwargs: Dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": body.price_id, "quantity": 1}],
        "success_url": body.success_url,
        "cancel_url": body.cancel_url,
        "client_reference_id": str(org_id),
        "allow_promotion_codes": True,
    }

    # Reuse an existing Stripe customer if we already have one (saves
    # the user from re-entering their email). Otherwise pre-fill the
    # email field so they don't have to type it.
    if org.get("stripe_customer_id"):
        checkout_kwargs["customer"] = org["stripe_customer_id"]
    else:
        checkout_kwargs["customer_email"] = user.email

    # Coupons are mutually exclusive with ``allow_promotion_codes`` in
    # Stripe's API — if the caller passed a coupon, drop the
    # promotion-codes flag and pass discounts explicitly.
    if body.coupon:
        checkout_kwargs.pop("allow_promotion_codes", None)
        checkout_kwargs["discounts"] = [{"coupon": body.coupon}]

    try:
        checkout = stripe.checkout.Session.create(**checkout_kwargs)
    except stripe.StripeError as exc:
        log.exception(
            "checkout: stripe API call failed for org=%s price=%s: %s",
            org_id,
            body.price_id,
            exc,
        )
        raise HTTPException(
            status_code=502, detail="Stripe API call failed"
        ) from exc

    log.info(
        "checkout: created session=%s for org=%s tier=%s",
        getattr(checkout, "id", "?"),
        org_id,
        price_to_tier[body.price_id],
    )
    return CheckoutResponse(checkout_url=checkout.url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal_session(
    request: Request,
    session: AsyncSession = Depends(_get_billing_session),
) -> PortalResponse:
    """Create a Stripe Customer Portal session.

    Returns ``{portal_url}`` on success. The frontend redirects the
    user to that URL so they can update payment method, change plan,
    or cancel.

    Errors:

    * 401 — caller is not authenticated.
    * 404 — the org has no Stripe customer yet (i.e. has never gone
      through checkout). The frontend should send the user to the
      pricing page instead of the portal.
    * 502 — Stripe API call failed.
    """
    _user, org_id = require_authed_org(request)
    _configure_stripe()

    org = await _fetch_org(session, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    stripe_customer_id = org.get("stripe_customer_id")
    if not stripe_customer_id:
        # No customer yet → can't open the portal. Frontend should
        # route to pricing page on this 404.
        raise HTTPException(
            status_code=404,
            detail="No Stripe customer for this organization",
        )

    try:
        portal_session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=get_settings().app_url + "/dashboard",
        )
    except stripe.StripeError as exc:
        log.exception(
            "portal: stripe API call failed for org=%s: %s", org_id, exc
        )
        raise HTTPException(
            status_code=502, detail="Stripe API call failed"
        ) from exc

    log.info("portal: created session for org=%s", org_id)
    return PortalResponse(portal_url=portal_session.url)
