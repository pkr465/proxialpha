"""Per-event-type handlers for Stripe webhooks.

Each ``handle_*`` function:

* Accepts an :class:`~sqlalchemy.ext.asyncio.AsyncSession` owned by the
  dispatcher (``webhook.py``), plus the already-parsed Stripe ``event``
  dict.
* Performs its DB mutations and returns ``None``.
* Never calls back out to the Stripe API. This process is a pure
  consumer — the webhook payload is authoritative for everything we
  need. (If you think you need a round-trip, the design is wrong; open
  an ADR amendment first.)
* Never commits. The dispatcher commits once per event, so a failure
  anywhere in the handler rolls the whole event back and leaves
  ``billing_raw.stripe_events.processed_at`` NULL for the next retry.

Price-to-tier mapping is resolved through
:func:`~api.billing.entitlement_seeder.build_price_to_tier_map`, which
reads the env-var names defined in ``config/tiers.yaml``. The map is
built fresh on every dispatch so tests that mutate ``os.environ`` see
the change immediately.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.entitlement_seeder import (
    build_price_to_tier_map,
    get_tier_config,
    seed_entitlements,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_to_dt(ts: Optional[int]) -> Optional[datetime]:
    """Convert a Stripe Unix timestamp to a naive UTC ``datetime``.

    We store timestamps as ``timestamptz`` in Postgres but pass them as
    naive UTC into asyncpg (which interprets naive as UTC). Keeping this
    helper in one place makes the code flow obvious and greppable.
    """
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)


def _resolve_tier_from_price(price_id: str) -> Optional[str]:
    """Look ``price_id`` up in the price→tier map; return None if unknown."""
    if not price_id:
        return None
    mapping = build_price_to_tier_map()
    return mapping.get(price_id)


def _first_line_item_price_id(subscription: Dict[str, Any]) -> Optional[str]:
    """Extract the recurring price ID from a subscription payload.

    Stripe's subscription object has ``items.data[*].price.id``. For the
    MVP we assume a single recurring line item (the tier plan). The
    metered overage item, if present, has a different ``recurring.usage_type``
    of ``metered`` and is filtered out here.
    """
    items = subscription.get("items") or {}
    data = items.get("data") or []
    for item in data:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        # Skip the metered overage line; we only want the licensed plan.
        if recurring.get("usage_type") == "metered":
            continue
        pid = price.get("id")
        if pid:
            return pid
    return None


def _collect_metered_item_ids(subscription: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{feature: subscription_item_id}`` for metered items.

    Task 05 needs these IDs to post usage records. The webhook handler
    stores them on ``subscriptions.metered_item_ids`` on creation so
    the metering job doesn't have to round-trip to Stripe.

    We key by the *feature name* (derived from the price lookup_key
    convention: ``signal_overage_trader`` → ``signals``). If a lookup
    key isn't set we fall back to the generic key ``signals`` since
    that's the only metered feature in v1.
    """
    out: Dict[str, str] = {}
    items = subscription.get("items") or {}
    data = items.get("data") or []
    for item in data:
        price = item.get("price") or {}
        recurring = price.get("recurring") or {}
        if recurring.get("usage_type") != "metered":
            continue
        sub_item_id = item.get("id")
        if not sub_item_id:
            continue
        # Crude feature inference — in v1 the only metered feature is
        # signals, so default to that. If we add more metered features
        # later, extend this via the lookup_key.
        lookup = (price.get("lookup_key") or "").lower()
        if "signal" in lookup or not lookup:
            out["signals"] = sub_item_id
        else:
            out[lookup] = sub_item_id
    return out


async def _get_org_id_by_stripe_customer(
    session: AsyncSession, stripe_customer_id: str
) -> Optional[uuid.UUID]:
    """Look up an org by its Stripe customer ID."""
    result = await session.execute(
        text("SELECT id FROM organizations WHERE stripe_customer_id = :sc"),
        {"sc": stripe_customer_id},
    )
    row = result.fetchone()
    if row is None:
        return None
    return uuid.UUID(str(row[0]))


async def _upsert_subscription(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    stripe_subscription_id: str,
    stripe_price_id: str,
    tier: str,
    status: str,
    seats: int,
    current_period_start: datetime,
    current_period_end: datetime,
    cancel_at_period_end: bool,
    metered_item_ids: Dict[str, str],
    now: datetime,
) -> None:
    """UPSERT one row in ``subscriptions`` keyed by stripe_subscription_id.

    Uses ``ON CONFLICT (stripe_subscription_id)`` which requires the
    Task 01 migration's UNIQUE constraint on that column.
    """
    import json

    await session.execute(
        text(
            """
            INSERT INTO subscriptions (
                id, org_id, stripe_subscription_id, stripe_price_id,
                status, tier, seats,
                current_period_start, current_period_end,
                cancel_at_period_end, metered_item_ids,
                created_at, updated_at
            )
            VALUES (
                :id, :org_id, :sub_id, :price_id,
                :status, :tier, :seats,
                :cps, :cpe,
                :cape, :mii,
                :now, :now
            )
            ON CONFLICT (stripe_subscription_id) DO UPDATE
            SET org_id               = EXCLUDED.org_id,
                stripe_price_id      = EXCLUDED.stripe_price_id,
                status               = EXCLUDED.status,
                tier                 = EXCLUDED.tier,
                seats                = EXCLUDED.seats,
                current_period_start = EXCLUDED.current_period_start,
                current_period_end   = EXCLUDED.current_period_end,
                cancel_at_period_end = EXCLUDED.cancel_at_period_end,
                metered_item_ids     = EXCLUDED.metered_item_ids,
                updated_at           = EXCLUDED.updated_at
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "org_id": str(org_id),
            "sub_id": stripe_subscription_id,
            "price_id": stripe_price_id,
            "status": status,
            "tier": tier,
            "seats": seats,
            "cps": current_period_start,
            "cpe": current_period_end,
            "cape": cancel_at_period_end,
            "mii": json.dumps(metered_item_ids),
            "now": now,
        },
    )


# ---------------------------------------------------------------------------
# checkout.session.completed
# ---------------------------------------------------------------------------


async def handle_checkout_completed(
    session: AsyncSession, event: Dict[str, Any]
) -> None:
    """Process ``checkout.session.completed``.

    Acceptance criteria from Task 02 prompt:

    * Only acts on ``mode == 'subscription'`` sessions.
    * Reads ``client_reference_id`` as the org_id (enforced in Task 03).
    * Sets ``organizations.stripe_customer_id`` if unset.
    * Upserts a ``subscriptions`` row.
    * Calls :func:`seed_entitlements` with the tier inferred from
      the price ID.

    The ``subscription`` field on a ``checkout.session.completed``
    payload may be either a nested object (when Stripe is configured to
    ``expand`` it) or a bare ID. This handler tolerates both:

    * Object form → read all fields directly.
    * ID form → emit a DEBUG log; we'll get the full data on the
      ``customer.subscription.created`` or ``.updated`` event that
      Stripe fires alongside checkout completion.

    Out-of-order delivery is fine: whichever event lands first
    initializes the org/sub, the other is a no-op at the price+tier
    level (and only mutates seats/status where different).
    """
    now = datetime.utcnow()
    data = event["data"]["object"]

    if data.get("mode") != "subscription":
        log.debug("checkout.session.completed ignored, mode=%s", data.get("mode"))
        return

    client_reference_id = data.get("client_reference_id")
    if not client_reference_id:
        raise ValueError(
            "checkout.session.completed missing client_reference_id; "
            "Task 03 must set this to the org_id when creating the session."
        )

    try:
        org_id = uuid.UUID(client_reference_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"client_reference_id is not a valid UUID: {client_reference_id!r}"
        ) from exc

    stripe_customer_id = data.get("customer")
    if not stripe_customer_id:
        raise ValueError("checkout.session.completed missing customer ID")

    # Set stripe_customer_id on the org if unset. Use COALESCE so we
    # don't clobber an existing linkage (e.g. second checkout after
    # churn reuses the same Stripe customer).
    await session.execute(
        text(
            "UPDATE organizations "
            "SET stripe_customer_id = COALESCE(stripe_customer_id, :sc), "
            "    updated_at = :now "
            "WHERE id = :org_id"
        ),
        {"sc": stripe_customer_id, "now": now, "org_id": str(org_id)},
    )

    subscription_field = data.get("subscription")
    if isinstance(subscription_field, dict):
        subscription = subscription_field
    else:
        # Subscription not expanded — log and defer. The subscription.*
        # webhook is already in flight and will land shortly.
        log.info(
            "checkout.session.completed for org=%s customer=%s: "
            "subscription not expanded, deferring to subscription.* event",
            org_id,
            stripe_customer_id,
        )
        return

    price_id = _first_line_item_price_id(subscription)
    if not price_id:
        raise ValueError(
            f"checkout.session.completed could not extract recurring price "
            f"from subscription {subscription.get('id')!r}"
        )

    tier = _resolve_tier_from_price(price_id)
    if not tier:
        raise ValueError(
            f"Unknown Stripe price ID {price_id!r} — not found in tiers.yaml"
        )

    cps = _ts_to_dt(subscription.get("current_period_start")) or now
    cpe = _ts_to_dt(subscription.get("current_period_end")) or now

    await _upsert_subscription(
        session,
        org_id=org_id,
        stripe_subscription_id=subscription["id"],
        stripe_price_id=price_id,
        tier=tier,
        status=subscription.get("status", "active"),
        seats=int(subscription.get("quantity") or 1),
        current_period_start=cps,
        current_period_end=cpe,
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
        metered_item_ids=_collect_metered_item_ids(subscription),
        now=now,
    )

    await seed_entitlements(
        session,
        org_id=org_id,
        tier=tier,
        period_start=cps,
        period_end=cpe,
        now=now,
    )

    log.info(
        "checkout.session.completed processed: org=%s tier=%s sub=%s",
        org_id,
        tier,
        subscription["id"],
    )


# ---------------------------------------------------------------------------
# customer.subscription.updated (and .created — same handler)
# ---------------------------------------------------------------------------


async def handle_subscription_updated(
    session: AsyncSession, event: Dict[str, Any]
) -> None:
    """Process ``customer.subscription.updated`` / ``.created``.

    * Updates status, seats, cancel_at_period_end, current_period_end.
    * If the tier changed (the *effective* Stripe price ID is now mapped
      to a different tier in ``tiers.yaml``), reseeds entitlements. The
      reseed is the point of the spec: upgrading mid-cycle gives the
      customer immediate access to the new tier's quota.
    """
    now = datetime.utcnow()
    subscription = event["data"]["object"]

    stripe_customer_id = subscription.get("customer")
    if not stripe_customer_id:
        raise ValueError("subscription event missing customer ID")

    org_id = await _get_org_id_by_stripe_customer(session, stripe_customer_id)
    if org_id is None:
        # Checkout hasn't landed yet. Stripe can deliver out of order;
        # next replay will find the row. For now this is a safe no-op.
        log.info(
            "subscription.updated for unknown customer=%s, deferring",
            stripe_customer_id,
        )
        return

    price_id = _first_line_item_price_id(subscription)
    if not price_id:
        raise ValueError(
            f"subscription.updated has no recurring price for "
            f"sub={subscription.get('id')!r}"
        )

    tier = _resolve_tier_from_price(price_id)
    if not tier:
        raise ValueError(
            f"Unknown Stripe price ID {price_id!r} on subscription update"
        )

    cps = _ts_to_dt(subscription.get("current_period_start")) or now
    cpe = _ts_to_dt(subscription.get("current_period_end")) or now

    # Detect tier change by reading the previous row.
    result = await session.execute(
        text(
            "SELECT tier FROM subscriptions "
            "WHERE stripe_subscription_id = :sid"
        ),
        {"sid": subscription["id"]},
    )
    row = result.fetchone()
    previous_tier = str(row[0]) if row else None

    await _upsert_subscription(
        session,
        org_id=org_id,
        stripe_subscription_id=subscription["id"],
        stripe_price_id=price_id,
        tier=tier,
        status=subscription.get("status", "active"),
        seats=int(subscription.get("quantity") or 1),
        current_period_start=cps,
        current_period_end=cpe,
        cancel_at_period_end=bool(subscription.get("cancel_at_period_end")),
        metered_item_ids=_collect_metered_item_ids(subscription),
        now=now,
    )

    if previous_tier != tier:
        log.info(
            "subscription tier change org=%s: %s → %s, reseeding entitlements",
            org_id,
            previous_tier,
            tier,
        )
        await seed_entitlements(
            session,
            org_id=org_id,
            tier=tier,
            period_start=cps,
            period_end=cpe,
            now=now,
        )
    else:
        log.info(
            "subscription.updated processed: org=%s tier=%s status=%s",
            org_id,
            tier,
            subscription.get("status"),
        )


# ---------------------------------------------------------------------------
# customer.subscription.deleted
# ---------------------------------------------------------------------------


async def handle_subscription_deleted(
    session: AsyncSession, event: Dict[str, Any]
) -> None:
    """Process ``customer.subscription.deleted``.

    * Sets the subscription row's status to ``canceled``.
    * Downgrades the org to Free.
    * Calls :func:`seed_entitlements` with tier=``free`` so the
      remaining quota becomes the Free tier values immediately.

    We keep the subscription row for audit; we do not delete it.
    """
    now = datetime.utcnow()
    subscription = event["data"]["object"]

    stripe_customer_id = subscription.get("customer")
    if not stripe_customer_id:
        raise ValueError("subscription.deleted missing customer ID")

    org_id = await _get_org_id_by_stripe_customer(session, stripe_customer_id)
    if org_id is None:
        log.warning(
            "subscription.deleted for unknown customer=%s, ignoring",
            stripe_customer_id,
        )
        return

    # Mark subscription canceled.
    await session.execute(
        text(
            "UPDATE subscriptions "
            "SET status = 'canceled', updated_at = :now "
            "WHERE stripe_subscription_id = :sid"
        ),
        {"now": now, "sid": subscription["id"]},
    )

    # Compute a "free period" — the PRD is silent on what period bounds
    # to use for a Free tier, so we use [now, now + 30 days] as a
    # rolling placeholder. The Task 04 entitlement decorator only cares
    # that ``period_end > now()``.
    from datetime import timedelta

    free_start = now
    free_end = now + timedelta(days=30)

    await seed_entitlements(
        session,
        org_id=org_id,
        tier="free",
        period_start=free_start,
        period_end=free_end,
        now=now,
    )

    log.info(
        "subscription.deleted processed: org=%s downgraded to free, sub=%s",
        org_id,
        subscription["id"],
    )


# ---------------------------------------------------------------------------
# invoice.paid
# ---------------------------------------------------------------------------


async def handle_invoice_paid(
    session: AsyncSession, event: Dict[str, Any]
) -> None:
    """Process ``invoice.paid``.

    Two cases:

    * **Recurring invoice** (subscription renewal): reseed entitlements
      for the new billing period so ``remaining`` resets to the tier's
      included amount.
    * **Metered usage invoice**: this would mark contributing
      ``usage_events`` as ``reported_at``, but that logic lives in
      Task 05's metering job. We leave a TODO marker here.

    We detect the invoice type by checking whether any line item has
    ``recurring.usage_type == 'metered'``. A pure-subscription renewal
    has only licensed lines.
    """
    now = datetime.utcnow()
    invoice = event["data"]["object"]

    stripe_customer_id = invoice.get("customer")
    if not stripe_customer_id:
        raise ValueError("invoice.paid missing customer ID")

    org_id = await _get_org_id_by_stripe_customer(session, stripe_customer_id)
    if org_id is None:
        log.warning(
            "invoice.paid for unknown customer=%s, ignoring",
            stripe_customer_id,
        )
        return

    subscription_id = invoice.get("subscription")
    if not subscription_id:
        log.debug("invoice.paid without subscription field, ignoring")
        return

    # Was this a metered invoice? We look at the lines.
    lines = (invoice.get("lines") or {}).get("data") or []
    has_metered = any(
        ((ln.get("price") or {}).get("recurring") or {}).get("usage_type")
        == "metered"
        for ln in lines
    )
    if has_metered:
        # TODO(task-05): mark contributing usage_events.reported_at and
        # associate the stripe_usage_record_id with them. Left intentionally
        # incomplete per Task 02 "Do not" list (no metering logic here).
        log.info(
            "invoice.paid: metered invoice for org=%s sub=%s — deferred to metering job",
            org_id,
            subscription_id,
        )

    # For renewals, look up the subscription row and reseed entitlements
    # with the new period.
    result = await session.execute(
        text(
            "SELECT tier, current_period_start, current_period_end "
            "FROM subscriptions "
            "WHERE stripe_subscription_id = :sid"
        ),
        {"sid": subscription_id},
    )
    row = result.fetchone()
    if row is None:
        log.info(
            "invoice.paid for sub=%s but no local subscription row — deferring",
            subscription_id,
        )
        return

    tier = str(row[0])
    cps = row[1] or now
    cpe = row[2] or now

    # The invoice itself carries the new period bounds on each line
    # item's ``period`` field. Prefer those when present because they
    # are authoritative for the *just-billed* period.
    inv_period_start: Optional[datetime] = None
    inv_period_end: Optional[datetime] = None
    for ln in lines:
        period = ln.get("period") or {}
        if period.get("start") and period.get("end"):
            inv_period_start = _ts_to_dt(period["start"])
            inv_period_end = _ts_to_dt(period["end"])
            break

    period_start = inv_period_start or cps
    period_end = inv_period_end or cpe

    await seed_entitlements(
        session,
        org_id=org_id,
        tier=tier,
        period_start=period_start,
        period_end=period_end,
        now=now,
    )

    log.info(
        "invoice.paid processed: org=%s tier=%s period=%s→%s",
        org_id,
        tier,
        period_start,
        period_end,
    )


# ---------------------------------------------------------------------------
# invoice.payment_failed
# ---------------------------------------------------------------------------


async def handle_invoice_payment_failed(
    session: AsyncSession, event: Dict[str, Any]
) -> None:
    """Process ``invoice.payment_failed``.

    Sets the subscription status to ``past_due``. **Does not** touch
    entitlements: the customer paid for the current period and keeps
    full access until the grace window expires (enforced by the agent
    license heartbeat in Task 06).
    """
    now = datetime.utcnow()
    invoice = event["data"]["object"]

    subscription_id = invoice.get("subscription")
    if not subscription_id:
        log.debug("invoice.payment_failed without subscription field")
        return

    await session.execute(
        text(
            "UPDATE subscriptions "
            "SET status = 'past_due', updated_at = :now "
            "WHERE stripe_subscription_id = :sid"
        ),
        {"now": now, "sid": subscription_id},
    )

    log.info(
        "invoice.payment_failed processed: sub=%s status→past_due",
        subscription_id,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


# Mapping from Stripe event type to handler. The webhook dispatcher looks
# events up here; anything not listed is silently acknowledged (we still
# insert into billing_raw.stripe_events for the audit log, but we don't
# act on it).
EVENT_HANDLERS = {
    "checkout.session.completed": handle_checkout_completed,
    "customer.subscription.created": handle_subscription_updated,
    "customer.subscription.updated": handle_subscription_updated,
    "customer.subscription.deleted": handle_subscription_deleted,
    "invoice.paid": handle_invoice_paid,
    "invoice.payment_failed": handle_invoice_payment_failed,
}
