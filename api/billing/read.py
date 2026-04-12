"""Read-only billing endpoints — currently just ``GET /api/entitlements``.

This is the dashboard's single source of truth for what the customer
gets in their current billing period. The frontend polls it on page
load and again whenever the user hits a 402 (so the "you have N left"
UI stays accurate).

Why a separate module from :mod:`api.billing.endpoints`?

* ``endpoints.py`` handles *write* operations (Checkout, Portal) and
  imports ``stripe``. This module only reads the DB and builds a
  pydantic response — no Stripe dependency, faster to import, easier
  to test in isolation.
* Clear separation lets us later add caching / CDN headers here
  without touching the write-path code.

Response shape
--------------

Matches ``docs/specs/phase1-entitlements-and-billing.md`` §7.4
verbatim. Important normalisations:

* Numeric quota features (``signals``, ``backtests``) come back as
  ``{"included": int, "remaining": int, "overage_enabled": bool}``.
* Cap-style features (``tickers``, ``strategy_slots``) come back as
  ``{"max": int}`` — we do not expose ``remaining`` for caps because
  caps don't consume.
* Boolean features (``live_trading``, ``live_perps``,
  ``custom_strategies``) are JSON booleans. Never 0/1.
* ``api_access`` is the string token from ``tiers.yaml`` (``none``,
  ``read``, ``read_write``, ``full``), NEVER an integer. The task
  prompt mentions an integer mapping but the spec itself stores it as
  a string enum — we follow the spec.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.billing.endpoints import _get_billing_session
from api.billing.entitlement_seeder import get_tier_config
from api.middleware.auth_stub import require_authed_org

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Feature classification
# ---------------------------------------------------------------------------

# Features that consume on use → returned as {included, remaining, overage_enabled}.
# Must match ``api.billing.entitlement_seeder._NUMERIC_FEATURES`` keys that
# are genuinely consumables (not caps).
_CONSUMABLE_FEATURES = ("signals", "backtests")

# Cap-style features → returned as {"max": N}. These live in the
# entitlements table (the seeder writes them) but are not consumed;
# the decorator / read path treats ``included`` as the enforced cap.
_CAP_FEATURE_TO_JSON = {
    "tickers": "tickers",
    "strategy_slots": "strategy_slots",
}

# Boolean features. Values are pulled from ``tiers.yaml`` via
# get_tier_config; they do not live in the entitlements table.
_BOOLEAN_FEATURES = ("live_trading", "live_perps", "custom_strategies")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_org_tier(
    session: AsyncSession, org_id: uuid.UUID
) -> Optional[str]:
    """Return the org's current tier name, or None if missing."""
    result = await session.execute(
        text("SELECT tier FROM organizations WHERE id = :id"),
        {"id": str(org_id)},
    )
    row = result.fetchone()
    return row[0] if row else None


async def _fetch_current_entitlements(
    session: AsyncSession, org_id: uuid.UUID
) -> list[dict]:
    """Return entitlement rows for the org's most-recent period.

    We pick the single latest ``period_start`` across all rows and
    return every row sharing it. This handles the edge case where two
    features are seeded in the same period but at slightly different
    ``updated_at`` timestamps.
    """
    result = await session.execute(
        text(
            """
            SELECT feature, included, remaining, overage_enabled,
                   period_start, period_end
            FROM entitlements
            WHERE org_id = :org_id
              AND period_start = (
                  SELECT MAX(period_start) FROM entitlements
                  WHERE org_id = :org_id
              )
            """
        ),
        {"org_id": str(org_id)},
    )
    rows = result.fetchall()
    return [
        {
            "feature": r[0],
            "included": int(r[1]),
            "remaining": int(r[2]),
            "overage_enabled": bool(r[3]),
            "period_start": r[4],
            "period_end": r[5],
        }
        for r in rows
    ]


def _as_iso(value: Any) -> Any:
    """Normalise a timestamp bind-result to an ISO string for JSON output.

    SQLAlchemy may hand us a ``datetime`` (Postgres) or a plain string
    (SQLite TEXT). Both serialise fine to JSON, but we want the wire
    format to be stable so we call ``.isoformat()`` when possible.
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/entitlements")
async def get_entitlements(
    request: Request,
    session: AsyncSession = Depends(_get_billing_session),
) -> Dict[str, Any]:
    """Return the current org's tier + entitlements for the active period.

    Response shape is pinned by the Phase 1 spec §7.4. Errors:

    * 401 — no auth context.
    * 404 — the authed org has no row (stale auth header).
    * 404 — the org has no entitlement rows (free tier before seeding,
      or a seeding gap — we return a minimal free-tier shape rather
      than 500 so the dashboard stays useful).
    """
    _user, org_id = require_authed_org(request)

    tier = await _fetch_org_tier(session, org_id)
    if tier is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        tier_conf = get_tier_config(tier)
    except KeyError:
        # Org row has a tier name we don't recognise. Should be
        # prevented by the CHECK constraint but we still defend.
        log.error("get_entitlements: unknown tier %r on org %s", tier, org_id)
        raise HTTPException(status_code=500, detail="Unknown tier configured")

    rows = await _fetch_current_entitlements(session, org_id)
    by_feature = {r["feature"]: r for r in rows}

    # Build the features block. For consumable features we prefer the
    # live entitlement row (which reflects usage); for cap features
    # and booleans we read directly from tier config. This keeps the
    # shape consistent even when the entitlements table hasn't been
    # seeded yet (e.g. free tier orgs).
    features: Dict[str, Any] = {}

    for feat in _CONSUMABLE_FEATURES:
        row = by_feature.get(feat)
        if row is not None:
            features[feat] = {
                "included": row["included"],
                "remaining": row["remaining"],
                "overage_enabled": row["overage_enabled"],
            }
        else:
            # Fallback to tier config if this feature has no row yet.
            included = int(tier_conf.get(f"{feat}_included", 0))
            features[feat] = {
                "included": included,
                "remaining": included,
                "overage_enabled": False,
            }

    # Cap features — pull from entitlement row (so they reflect
    # per-tier overrides if we ever customise) OR tier config fallback.
    for ent_feat, json_key in _CAP_FEATURE_TO_JSON.items():
        row = by_feature.get(ent_feat)
        if row is not None:
            max_value = row["included"]
        else:
            max_value = int(tier_conf.get(f"{json_key}_max", 0))
        features[json_key] = {"max": max_value}

    # Boolean features — always from tier config.
    for bfeat in _BOOLEAN_FEATURES:
        features[bfeat] = bool(tier_conf.get(bfeat, False))

    # api_access — string enum, never an integer.
    features["api_access"] = str(tier_conf.get("api_access", "none"))

    # Period boundaries — if we have any row, use its period; else
    # fall back to None (free-tier never-seeded case).
    period_start: Any = None
    period_end: Any = None
    if rows:
        period_start = _as_iso(rows[0]["period_start"])
        period_end = _as_iso(rows[0]["period_end"])

    return {
        "tier": tier,
        "period_start": period_start,
        "period_end": period_end,
        "features": features,
    }
