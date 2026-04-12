"""Non-consuming entitlement gates for boolean flags and cap features.

This module is the *sibling* of :mod:`core.entitlements`. The latter
owns the load-bearing atomic ``UPDATE`` for consumables (signals,
backtests). This module owns the **read-only** gates for the other two
classes of features defined in ``config/tiers.yaml``:

* **Boolean flags** — ``live_trading``, ``live_perps``,
  ``custom_strategies``. These do not live in the ``entitlements``
  table; they are looked up directly from the org's current ``tier``
  via ``tiers.yaml``.

* **Cap features** — ``tickers``, ``strategy_slots``. These are
  written to the entitlements table by the seeder (so the dashboard's
  ``GET /api/entitlements`` can return them) but they never decrement.
  Enforcement is "current count + 1 <= included" at the call site.

Why a separate module from ``core.entitlements``?
-------------------------------------------------

The consumable path (try_consume) MUST run as a single SQL statement
under tenant RLS. The boolean and cap paths are simple SELECTs that do
not need that machinery. Mixing them into try_consume would muddy the
contract: a caller using ``requires_entitlement("live_trading", 1)``
would silently decrement a flag, which is nonsense. By keeping the
gates separate the call sites stay obvious and the SQL stays simple.

This module is the P1-3 fix for the gap analysis finding "legacy
trading paths are not wired to the entitlements gate". The gates are
designed to be called from inside route handlers (not as decorators)
because cap checks need access to the route's own state — e.g., the
current size of the watchlist for the tickers cap.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tier config lookup
# ---------------------------------------------------------------------------


def _tier_config(tier: str) -> Optional[dict]:
    """Return the parsed tier config from ``tiers.yaml`` or None.

    Wrapped in try/except so a checkout missing pyyaml does not crash
    the route — the gate falls open in that case (logged) rather than
    blocking legitimate paying customers behind a config error. The
    dependency error itself is the real bug to fix.
    """
    try:
        from api.billing.entitlement_seeder import get_tier_config
    except Exception as exc:  # pragma: no cover - optional dep fallback
        log.warning("feature_gates: tier config unavailable: %s", exc)
        return None
    try:
        return get_tier_config(tier)
    except KeyError:
        return None


async def _fetch_org_tier(
    session: AsyncSession, org_id: uuid.UUID
) -> Optional[str]:
    result = await session.execute(
        text("SELECT tier FROM organizations WHERE id = :id"),
        {"id": str(org_id)},
    )
    row = result.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Public gates
# ---------------------------------------------------------------------------


# The set of boolean keys we currently enforce. Adding a new boolean
# feature only needs an entry here PLUS a key in tiers.yaml — the
# enforcement helper itself is generic.
BOOLEAN_FEATURE_KEYS = frozenset({"live_trading", "live_perps", "custom_strategies"})


# Map cap features (used by callers) to the YAML key that holds the
# limit. The entitlements table mirrors these as ``included`` rows.
CAP_FEATURE_TO_YAML_KEY = {
    "tickers": "tickers_max",
    "strategy_slots": "strategy_slots_max",
}


async def assert_feature_flag(
    session: AsyncSession,
    org_id: uuid.UUID,
    feature: str,
) -> None:
    """Raise 403 if the org's current tier does not include ``feature``.

    Used for boolean tier flags like ``live_trading``. The check is a
    single SELECT against ``organizations`` plus an in-memory dict
    lookup against the loaded ``tiers.yaml``. No write, no transaction
    state, safe to call from any handler.

    Why 403 and not 402? 402 means "you've exhausted a quota you
    bought" — the right response for the user is to upgrade or wait
    for the next period. 403 means "your current plan does not
    include this feature at all" — the right response is to upgrade.
    The dashboard renders these two states differently.
    """
    if feature not in BOOLEAN_FEATURE_KEYS:
        raise ValueError(
            f"assert_feature_flag: {feature!r} is not a boolean feature; "
            f"known booleans: {sorted(BOOLEAN_FEATURE_KEYS)}"
        )

    tier = await _fetch_org_tier(session, org_id)
    if tier is None:
        # Defensive: an authed request whose org row vanished. Block.
        raise HTTPException(
            status_code=403,
            detail={"error": "feature_not_available", "feature": feature},
        )

    conf = _tier_config(tier)
    if conf is None:
        # Tier YAML missing or unparseable. Fail closed for booleans —
        # we would rather block a legitimate request than allow a paid
        # feature to leak into a free tier.
        log.warning(
            "assert_feature_flag: tier %r config missing, blocking %s",
            tier,
            feature,
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "feature_not_available", "feature": feature},
        )

    if not bool(conf.get(feature, False)):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_not_available",
                "feature": feature,
                "tier": tier,
                "upgrade_required": True,
            },
        )


async def assert_within_cap(
    session: AsyncSession,
    org_id: uuid.UUID,
    feature: str,
    *,
    proposed_count: int,
) -> None:
    """Raise 402 if ``proposed_count`` would exceed the cap for ``feature``.

    Used for cap-style features like ``tickers``. Caller passes the
    *proposed* total (e.g. ``current_watchlist_size + 1``) and we
    block if it exceeds the tier's ``included`` value.

    The cap is read from the org's tier in ``tiers.yaml`` (NOT from
    the entitlements row) so that mid-period upgrades take effect
    immediately. The entitlements row is the period-frozen mirror; the
    YAML is the live source of truth for caps.

    Why 402 here and not 403? Caps share the "you can buy more by
    upgrading" UX with consumables, so the dashboard handles 402 the
    same way for both. Reserve 403 for boolean tier flags only.
    """
    if feature not in CAP_FEATURE_TO_YAML_KEY:
        raise ValueError(
            f"assert_within_cap: {feature!r} is not a cap feature; "
            f"known caps: {sorted(CAP_FEATURE_TO_YAML_KEY)}"
        )

    tier = await _fetch_org_tier(session, org_id)
    if tier is None:
        raise HTTPException(
            status_code=402,
            detail={"error": "quota_exhausted", "feature": feature},
        )

    conf = _tier_config(tier)
    if conf is None:
        # Same fail-closed posture as the boolean gate — caps are
        # paid features and we don't want a config blip to hand out
        # unlimited capacity.
        log.warning(
            "assert_within_cap: tier %r config missing, blocking %s",
            tier,
            feature,
        )
        raise HTTPException(
            status_code=402,
            detail={"error": "quota_exhausted", "feature": feature},
        )

    yaml_key = CAP_FEATURE_TO_YAML_KEY[feature]
    cap = int(conf.get(yaml_key, 0))
    if proposed_count > cap:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "quota_exhausted",
                "feature": feature,
                "cap": cap,
                "proposed": proposed_count,
                "tier": tier,
            },
        )
