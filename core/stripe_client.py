"""Thin wrapper around the Stripe SDK for the control plane.

This module exists to give the rest of the codebase a single, mockable
surface for outbound Stripe calls. Task 05's metering job talks to
Stripe through this module, and tests monkey-patch the callable rather
than the raw ``stripe`` namespace — which keeps test setup short and
makes the seam obvious.

Scope
-----

Phase 1 only needs ONE outbound call: posting usage records for
metered subscription items. This module therefore exposes exactly one
function, :func:`report_usage`, plus the :class:`StripeReportError`
that callers should catch.

Design notes
~~~~~~~~~~~~

* The module lazily configures ``stripe.api_key`` from
  :func:`core.settings.get_settings` the first time it's called. Tests
  that monkey-patch the function never hit this path.
* ``action="set"`` is used (not ``"increment"``) so that a retry with
  the same idempotency key is a true no-op: Stripe records the total
  usage for the period, not a delta. This is load-bearing — ``"increment"``
  would double-count on retry even with the idempotency key.
* The ``idempotency_key`` is passed both to the Stripe SDK (as an HTTP
  header) AND stored in our ``usage_events.idempotency_key`` column.
  The two-way consistency is what lets the metering job replay safely.
* The public function never raises raw ``stripe.*`` exceptions at
  callers. Every outbound error is re-raised as
  :class:`StripeReportError`, which lets the metering job have a
  single-except clause without importing the Stripe SDK.

Why not put this directly in ``jobs/meter_usage.py``? Because the
metering job is a batch process with its own retry loop and logging
concerns, and the Stripe wire call is stateless. Keeping them in
separate modules means we can unit-test the metering logic without
any Stripe mocks (we pass a fake callable in as a dependency).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class StripeReportError(RuntimeError):
    """Raised when an outbound Stripe call fails.

    Wraps the underlying ``stripe.StripeError`` (if any) so callers can
    catch a single concrete type. The original exception is attached as
    ``__cause__`` for traceback introspection.
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# API-key plumbing
# ---------------------------------------------------------------------------


_configured = False


def _configure() -> None:
    """Set ``stripe.api_key`` from settings exactly once per process.

    Separated so tests that monkey-patch :func:`report_usage` never
    import ``core.settings`` (which would load .env and slow the
    suite). Real callers hit this on the first live call.
    """
    global _configured
    if _configured:
        return
    import stripe  # local import — expensive, do it only when needed

    from core.settings import get_settings

    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    _configured = True


# ---------------------------------------------------------------------------
# Public API — usage reporting
# ---------------------------------------------------------------------------


def report_usage(
    subscription_item_id: str,
    quantity: int,
    *,
    idempotency_key: str,
    timestamp: Optional[int] = None,
) -> Dict[str, Any]:
    """Post a usage record to Stripe for a metered subscription item.

    Parameters
    ----------
    subscription_item_id
        The Stripe ``si_...`` ID from ``subscriptions.metered_item_ids``.
        The webhook handler stores one per metered feature.
    quantity
        Non-negative integer. This is the *absolute* usage for the
        hour bucket (``action="set"``), not a delta.
    idempotency_key
        Deterministic string: ``usage_{org_id}_{feature}_{bucket_epoch}``.
        Stripe uses this to deduplicate retries; we also store it on
        ``usage_events`` so a batch retry recovers the same record.
    timestamp
        Optional Unix timestamp (UTC) for the bucket. Stripe stores it
        on the record; if omitted, Stripe uses ``now``. We always pass
        the bucket's floor so retries land on the same period.

    Returns
    -------
    dict
        The Stripe usage record object, with at least ``id`` populated.
        Callers should persist ``id`` onto
        ``usage_events.stripe_usage_record_id`` for audit.

    Raises
    ------
    StripeReportError
        On any Stripe SDK error (network, auth, invalid ID, rate limit,
        etc). The original exception is attached as ``__cause__``.
    """
    if quantity < 0:
        raise ValueError(f"quantity must be >= 0, got {quantity!r}")
    if not subscription_item_id:
        raise ValueError("subscription_item_id is required")
    if not idempotency_key:
        raise ValueError("idempotency_key is required")

    _configure()

    import stripe  # local import keeps module import cheap for tests

    kwargs: Dict[str, Any] = {
        "quantity": int(quantity),
        "action": "set",
        "idempotency_key": idempotency_key,
    }
    if timestamp is not None:
        kwargs["timestamp"] = int(timestamp)

    try:
        record = stripe.SubscriptionItem.create_usage_record(
            subscription_item_id, **kwargs
        )
    except stripe.error.StripeError as exc:  # type: ignore[attr-defined]
        status = getattr(exc, "http_status", None)
        log.warning(
            "stripe_client: create_usage_record failed si=%s qty=%s idem=%s: %s",
            subscription_item_id,
            quantity,
            idempotency_key,
            exc,
        )
        raise StripeReportError(
            f"create_usage_record failed: {exc}", status=status
        ) from exc
    except Exception as exc:  # defensive: unexpected SDK internals
        log.exception(
            "stripe_client: unexpected error on create_usage_record si=%s",
            subscription_item_id,
        )
        raise StripeReportError(f"unexpected Stripe SDK error: {exc}") from exc

    # The Stripe SDK returns a ``StripeObject`` — coerce to a plain dict
    # so callers don't accidentally depend on the SDK type.
    if hasattr(record, "to_dict"):
        return dict(record.to_dict())
    return dict(record)


__all__ = ["StripeReportError", "report_usage"]
