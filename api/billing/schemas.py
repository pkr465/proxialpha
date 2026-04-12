"""Pydantic request/response models for the Billing endpoints.

Used by :mod:`api.billing.endpoints`. Kept in their own module so the
endpoint module stays focused on routing/Stripe-call logic and so
schemas can be imported independently by tests and (eventually) an
OpenAPI client generator.

Design notes
------------

*   We deliberately do *not* model every Stripe field — only the small
    subset our frontend needs. The full Stripe Session/Portal objects
    are huge and we never want to leak more than necessary.

*   ``CheckoutRequest.price_id`` is a free-form string here; the
    endpoint validates it against the env-mapped allow-list built from
    ``config/tiers.yaml``. Doing the validation in the endpoint rather
    than the schema lets us return a structured 400 with the list of
    accepted prices, which is more useful than a Pydantic
    ``ValidationError``.

*   ``ActiveSubscriptionError`` is the JSON body returned with a 409
    when the org already has an active/trialing/past_due subscription.
    The frontend reads ``portal_url`` and redirects the user to manage
    their existing sub instead of creating a new one.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    """Body for ``POST /api/billing/checkout``.

    The frontend constructs this from the pricing page. ``price_id`` is
    the Stripe ``price_...`` ID for the tier the user clicked. Both
    ``success_url`` and ``cancel_url`` are required so we don't have to
    bake frontend routing knowledge into the backend.
    """

    price_id: str = Field(
        ...,
        description="Stripe price ID (e.g. 'price_1OabcXYZ'). Validated server-side.",
    )
    success_url: str = Field(
        ...,
        description="URL Stripe redirects to after successful checkout.",
    )
    cancel_url: str = Field(
        ...,
        description="URL Stripe redirects to if the user cancels checkout.",
    )
    coupon: Optional[str] = Field(
        default=None,
        description="Optional Stripe coupon ID to apply at checkout time.",
    )


class CheckoutResponse(BaseModel):
    """Body returned by ``POST /api/billing/checkout`` on success.

    The frontend immediately ``window.location = checkout_url`` after
    receiving this. We do not return the Stripe session object — only
    the redirect URL the user actually needs.
    """

    checkout_url: str = Field(
        ...,
        description="Hosted Stripe Checkout URL — redirect the user here.",
    )


class PortalResponse(BaseModel):
    """Body returned by ``POST /api/billing/portal`` on success."""

    portal_url: str = Field(
        ...,
        description="Hosted Stripe Customer Portal URL — redirect the user here.",
    )


class ActiveSubscriptionError(BaseModel):
    """409 body when checkout is rejected because a sub already exists.

    Returned with HTTP 409. The frontend should redirect the user to
    ``portal_url`` so they can change plans through the Customer Portal
    instead of creating a duplicate subscription.
    """

    detail: str = Field(
        default="Organization already has an active subscription.",
        description="Human-readable error message.",
    )
    portal_url: Optional[str] = Field(
        default=None,
        description="Customer Portal URL where the user can manage the existing sub.",
    )
