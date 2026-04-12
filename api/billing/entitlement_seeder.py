"""Seed the ``entitlements`` table from ``config/tiers.yaml``.

This module owns the one operation: given (org_id, tier, period_start,
period_end), upsert one row in ``entitlements`` per quota feature with
the tier's included amount and ``remaining`` reset to that included
amount.

Design notes
------------

*   **Idempotent.** Uses ``ON CONFLICT (org_id, feature, period_start)
    DO UPDATE`` so replaying the same webhook produces the same state.
    The unique constraint was declared in the Task 01 migration:
    ``uq_entitlements_org_feature_period``.

*   **Feature set is tier-specific.** We only insert *numeric* quota
    features (``signals``, ``backtests``, ``tickers``, ``strategy_slots``).
    Boolean features (``live_trading``, ``live_perps`` …) are enforced
    at decorator level via a separate lookup — we do not pollute the
    entitlements table with 0/1 booleans, which would be a worse fit for
    the ``remaining`` column.

*   **Overage.** The ``overage_enabled`` column is set to ``True`` for
    the ``signals`` feature whenever the tier has an ``overage_price``
    configured. Other features (backtests, tickers, slots) have no
    overage path — they just return 402 when exhausted.

*   **Postgres-first, SQLite-tolerant.** The upsert SQL is written with
    ``ON CONFLICT`` which SQLite 3.35+ supports, so the same statement
    runs under the test conftest (SQLite) and production (Postgres)
    without branching.

Loading of ``tiers.yaml`` happens **once** at module import, not per
call. Tests that need to simulate a different tier table can patch
:data:`_TIERS` directly.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Tier config loading
# ---------------------------------------------------------------------------

# Resolve ``config/tiers.yaml`` relative to the repo root. We walk up from
# this file rather than relying on CWD, which differs between ``pytest``,
# ``uvicorn``, and ``alembic``.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TIERS_PATH = _REPO_ROOT / "config" / "tiers.yaml"


def _load_tiers() -> Dict[str, Dict[str, Any]]:
    """Load ``config/tiers.yaml`` and return its ``tiers`` mapping."""
    with _TIERS_PATH.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict) or "tiers" not in raw:
        raise ValueError(
            f"{_TIERS_PATH} is malformed: expected top-level 'tiers' key."
        )
    tiers = raw["tiers"]
    if not isinstance(tiers, dict):
        raise ValueError(f"{_TIERS_PATH} 'tiers' must be a mapping.")
    return tiers


# Module-level cache. Tests can ``api.billing.entitlement_seeder._TIERS = {...}``
# to monkey-patch without touching the filesystem.
_TIERS: Dict[str, Dict[str, Any]] = _load_tiers()


def get_tier_config(tier: str) -> Dict[str, Any]:
    """Return the parsed config for ``tier`` or raise ``KeyError``."""
    if tier not in _TIERS:
        raise KeyError(
            f"Unknown tier {tier!r}; known tiers: {sorted(_TIERS.keys())}"
        )
    return _TIERS[tier]


# ---------------------------------------------------------------------------
# Price-to-tier lookup (used by handlers.py)
# ---------------------------------------------------------------------------


def build_price_to_tier_map() -> Dict[str, str]:
    """Return ``{stripe_price_id: tier_name}`` from env + ``tiers.yaml``.

    The YAML stores env var *names* (e.g. ``STRIPE_PRICE_TRADER_MONTHLY``)
    rather than literal IDs. This helper resolves them at call time so
    tests can set env vars before building the map.

    Returns an empty dict if none of the env vars are set — in that case
    the webhook handler will fall back to the ``lookup_key`` or raise.
    Missing env vars are logged by the caller, not here.
    """
    out: Dict[str, str] = {}
    for tier_name, conf in _TIERS.items():
        prices = conf.get("stripe_prices")
        if not prices:
            continue
        for _period, env_name in prices.items():
            price_id = os.environ.get(env_name)
            if price_id:
                out[price_id] = tier_name
    return out


# ---------------------------------------------------------------------------
# Seeding SQL
# ---------------------------------------------------------------------------

# Mapping from tier YAML key → entitlements.feature value. Only numeric
# quotas that consume on use live in entitlements; booleans and caps
# (like tickers_max) are enforced at the decorator level by reading
# tier config directly.
#
# Why include tickers and strategy_slots here even though they are caps
# and not consumables? Because the decorator + usage tracking layer
# (Task 04) reads the ``included`` column as the enforced cap — so we
# write them to the same table for uniform access.
_NUMERIC_FEATURES = (
    ("signals", "signals_included"),
    ("backtests", "backtests_included"),
    ("tickers", "tickers_max"),
    ("strategy_slots", "strategy_slots_max"),
)


# The webhook handler runs with BYPASSRLS privileges (via a background
# worker role) and sets org_id explicitly on every insert. It does NOT
# use ``core.db.get_session`` which requires a tenant context — the
# handler manages its own session lifecycle.
_UPSERT_ENTITLEMENT_SQL = text(
    """
    INSERT INTO entitlements (
        id, org_id, feature, period_start, period_end,
        included, remaining, overage_enabled, updated_at
    )
    VALUES (
        :id, :org_id, :feature, :period_start, :period_end,
        :included, :included, :overage_enabled, :now
    )
    ON CONFLICT (org_id, feature, period_start) DO UPDATE
    SET included        = EXCLUDED.included,
        remaining       = EXCLUDED.included,
        period_end      = EXCLUDED.period_end,
        overage_enabled = EXCLUDED.overage_enabled,
        updated_at      = EXCLUDED.updated_at
    """
)


async def seed_entitlements(
    session: AsyncSession,
    org_id: uuid.UUID,
    tier: str,
    period_start: datetime,
    period_end: datetime,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Upsert one ``entitlements`` row per numeric feature for ``tier``.

    * ``session`` — an open AsyncSession in a transaction owned by the
      caller (usually the webhook handler). This function does NOT
      commit; the webhook handler commits once per event.
    * ``org_id`` — already validated by the caller.
    * ``tier`` — must be a key in ``tiers.yaml``. Unknown tier raises.
    * ``period_start`` / ``period_end`` — billing period boundaries,
      typically from the Stripe subscription's current_period_*.
    * ``now`` — override for tests (default: ``datetime.utcnow()``).

    Replay safety: calling this twice with the same arguments is a
    no-op (values are idempotent). Calling it twice with a *different*
    tier for the same period resets the ``remaining`` counter to the
    new tier's included amount — this is intentional for tier upgrades
    mid-period.
    """
    conf = get_tier_config(tier)
    overage_for_signals = bool(conf.get("overage_price"))
    stamp = now or datetime.utcnow()

    for feature, yaml_key in _NUMERIC_FEATURES:
        included = int(conf.get(yaml_key, 0))
        overage_enabled = feature == "signals" and overage_for_signals
        await session.execute(
            _UPSERT_ENTITLEMENT_SQL,
            {
                "id": str(uuid.uuid4()),
                "org_id": str(org_id),
                "feature": feature,
                "period_start": period_start,
                "period_end": period_end,
                "included": included,
                "overage_enabled": overage_enabled,
                "now": stamp,
            },
        )

    # Also update organizations.tier so the dashboard reflects the
    # effective tier immediately. This is load-bearing: the PRD §10
    # calls out that ``organizations.tier`` is the effective tier for
    # display purposes.
    await session.execute(
        text(
            "UPDATE organizations "
            "SET tier = :tier, updated_at = :now "
            "WHERE id = :org_id"
        ),
        {"tier": tier, "now": stamp, "org_id": str(org_id)},
    )
