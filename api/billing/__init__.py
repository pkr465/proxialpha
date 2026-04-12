"""Billing subpackage — Stripe webhook + Checkout/Portal endpoints.

* Task 02 (``api.billing.webhook``) implements ``POST /webhook`` and
  the event-type handlers in ``api.billing.handlers``.
* Task 03 (``api.billing.endpoints``) implements ``POST /checkout``
  and ``POST /portal``.

Both routers are combined into :data:`billing_router` and mounted
under ``/api/billing`` by :mod:`api.server`. We use a single combined
router (rather than two ``include_router`` calls) so the FastAPI
mount point only needs to know about one symbol.
"""
from __future__ import annotations

from fastapi import APIRouter

from api.billing.endpoints import router as _endpoints_router
from api.billing.read import router as read_router  # noqa: F401  (re-exported)
from api.billing.webhook import router as _webhook_router

# Combined router for the ``/api/billing`` mount. The ``read_router``
# is intentionally NOT included here — it exposes ``GET /entitlements``
# which is mounted at the top-level ``/api`` path (per spec §7.4), not
# under ``/api/billing``. ``api.server`` imports ``read_router``
# separately and mounts it at ``/api``.
billing_router = APIRouter()
billing_router.include_router(_webhook_router)
billing_router.include_router(_endpoints_router)

__all__ = ["billing_router"]
