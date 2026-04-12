"""Atomic entitlement consumption + the ``requires_entitlement`` decorator.

This module is the runtime gate between a paying customer and the
metered features they bought. Every code path that consumes a quota
(AI signal generation, backtest runs, live-trading activations …)
must route through :func:`try_consume` — either directly or via
:func:`requires_entitlement` on a FastAPI route.

The whole design is built around ONE invariant: under concurrent load,
the total number of successful consumes can never exceed the
entitlement's ``remaining`` column at the start of the period. This
is achieved by a single atomic ``UPDATE ... RETURNING`` statement —
no read-modify-write pattern, no application-level locking, no Redis.
Postgres' per-row lock in the UPDATE is the only synchronisation
primitive needed.

Phase 1 deliberate non-features
-------------------------------

* No in-process caching of entitlements. Every consume hits the DB.
  Caching is a later optimisation (Phase 3 sketch: read-through cache
  with write-invalidate on the webhook handler). For now: correctness
  first.
* No soft cap on overage. When ``overage_enabled = true`` the user can
  drive ``remaining`` arbitrarily negative. Finance will catch it via
  the monthly Stripe invoice — this is acceptable under the Phase 1
  PRD (§12).
* No bulk consume / batching. Each call is one row, one statement.

Cross-dialect SQL
-----------------

The same SQL runs under Postgres (production) and SQLite (tests). We
avoid Postgres-isms that SQLite can't parse:

* ``CURRENT_TIMESTAMP`` instead of ``now()``.
* ``overage_enabled`` as a bare truthy expression instead of
  ``overage_enabled = true`` (SQLite stores booleans as INTEGER 0/1).
* ``period_end`` comparisons use a Python-supplied bound value (ISO
  string or datetime) rather than ``now()`` at the server so the
  comparison semantics are identical in both dialects.

"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Optional,
)

from fastapi import HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of a :func:`try_consume` call.

    Fields:

    * ``allowed`` — True if the consume was applied to the DB. False
      means the caller should surface a 402 / quota-exhausted response.
    * ``remaining`` — The new ``remaining`` value *after* the consume.
      May be negative when ``is_overage`` is True. Zero when
      ``allowed`` is False (we don't know the real remaining in the
      blocked case and don't want to leak it).
    * ``is_overage`` — True iff the consume was allowed AND the new
      remaining is below zero. A Task 05 reporting job will pick up
      these rows from ``usage_events`` and meter them to Stripe.
    """

    allowed: bool
    remaining: int
    is_overage: bool


# ---------------------------------------------------------------------------
# Session plumbing for the decorator
# ---------------------------------------------------------------------------

# The decorator needs to acquire a DB session without going through
# ``core.db.get_session`` (which requires a tenant context ContextVar).
# We expose a tiny lazy-engine helper here that tests can override by
# assigning ``core.entitlements._session_factory`` directly.
#
# Production: lazy engine + async_sessionmaker from settings.
# Tests: monkey-patch ``_session_factory`` to yield a fixture session.
_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_or_create_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return a lazily-constructed session factory pointed at the prod DB."""
    global _engine, _sessionmaker
    if _sessionmaker is None:
        # Import here so importing this module does not require
        # pydantic-settings during tests that monkey-patch the factory.
        from core.settings import get_settings

        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, pool_size=5
        )
        _sessionmaker = async_sessionmaker(
            bind=_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


@asynccontextmanager
async def _default_session_factory() -> AsyncIterator[AsyncSession]:
    """Open a short-lived AsyncSession for the decorator to use."""
    maker = _get_or_create_sessionmaker()
    session = maker()
    try:
        yield session
    finally:
        await session.close()


# Tests override this with a callable returning an
# ``AsyncContextManager[AsyncSession]``. Using a module-level reference
# rather than an import hook keeps the override surface small and
# discoverable: ``entitlements._session_factory = my_fixture``.
_session_factory: Callable[[], Any] = _default_session_factory


# ---------------------------------------------------------------------------
# Overage unit cost lookup
# ---------------------------------------------------------------------------

# Per-unit overage cost in USD, by (tier, feature). This table is
# informational only — the Stripe price is the source of truth for
# what the customer is actually charged. We store ``cost_usd`` so that
# support agents can answer "why was I billed $X?" without asking
# Stripe. Numbers here should be kept in sync with whatever the
# Stripe product's unit_amount is set to.
#
# TODO(phase1-task5): move this into ``config/tiers.yaml`` alongside
# the ``overage_price`` env var. For now a hard-coded map is fine —
# Task 05 will replace this with a real lookup and start metering.
_OVERAGE_UNIT_COST_USD: Dict[tuple, float] = {
    ("trader", "signals"): 0.02,
    ("pro", "signals"): 0.01,
    ("team", "signals"): 0.005,
}


async def _resolve_tier_for_org(
    session: AsyncSession, org_id: uuid.UUID
) -> Optional[str]:
    """Return ``organizations.tier`` or None if the org row is missing.

    Used by the overage path to pick the right unit cost. Not required
    for the main consume path — we already have the entitlement row.
    """
    result = await session.execute(
        text("SELECT tier FROM organizations WHERE id = :id"),
        {"id": str(org_id)},
    )
    row = result.fetchone()
    return row[0] if row else None


def _lookup_overage_cost(tier: Optional[str], feature: str) -> Optional[float]:
    """Unit cost in USD for one unit of overage, or None if unknown."""
    if tier is None:
        return None
    return _OVERAGE_UNIT_COST_USD.get((tier, feature))


# ---------------------------------------------------------------------------
# Core atomic consume
# ---------------------------------------------------------------------------


# The load-bearing UPDATE. Three clauses matter:
#
# 1. ``period_end > :now`` — never consume from a stale/past-period
#    row. If this clause finds nothing, the seeder dropped a beat and
#    we fail loudly (the caller gets allowed=False) rather than
#    silently double-counting last month.
#
# 2. ``remaining >= :qty OR overage_enabled`` — allow the update if
#    either the tier has capacity OR the tier is configured to bill
#    overage. We rely on Postgres evaluating the WHERE against the
#    pre-update row value, which is the standard SQL semantics.
#
# 3. ``RETURNING remaining, overage_enabled, id`` — we need the
#    *post*-update ``remaining`` to decide is_overage, plus the row
#    id for logging and the overage flag for the caller.
_CONSUME_SQL = text(
    """
    UPDATE entitlements
    SET remaining = remaining - :qty,
        updated_at = CURRENT_TIMESTAMP
    WHERE org_id = :org_id
      AND feature = :feature
      AND period_end > :now
      AND (remaining >= :qty OR overage_enabled)
    RETURNING remaining, overage_enabled, id
    """
)

_PEEK_SQL = text(
    """
    SELECT remaining, overage_enabled, included, period_end
    FROM entitlements
    WHERE org_id = :org_id
      AND feature = :feature
      AND period_end > :now
    ORDER BY period_start DESC
    LIMIT 1
    """
)

_INSERT_USAGE_EVENT_SQL = text(
    """
    INSERT INTO usage_events (
        id, org_id, feature, quantity, cost_usd, billed,
        idempotency_key, occurred_at
    )
    VALUES (
        :id, :org_id, :feature, :qty, :cost_usd, 1,
        :idempotency_key, CURRENT_TIMESTAMP
    )
    """
)


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp suitable as a bind parameter.

    Why ISO string not ``datetime`` object? Because our SQLite test
    schema stores ``period_end`` as TEXT — lexicographic comparison
    against an ISO string is correct *only* if both sides use the same
    format. Using a Python datetime works in Postgres but yields
    format mismatches in SQLite. Fixing it here keeps the core SQL
    dialect-free.
    """
    return datetime.now(timezone.utc).isoformat()


async def try_consume(
    session: AsyncSession,
    org_id: uuid.UUID,
    feature: str,
    qty: int,
    *,
    idempotency_key: Optional[str] = None,
) -> ConsumeResult:
    """Atomically decrement the current period's entitlement.

    Runs a single ``UPDATE ... RETURNING`` — see module docstring for
    the concurrency invariant. Callers MUST pass an open ``session``
    and are responsible for committing it after this function returns;
    we do not commit on their behalf because the caller may want to
    batch work into the same transaction.

    On an overage path (``remaining`` went negative) we also insert
    one row into ``usage_events`` with ``billed=true`` so Task 05 can
    meter it to Stripe. The insert uses ``idempotency_key`` if
    provided or a fresh UUID otherwise; if the key collides with an
    existing row (rare — usually indicates a client retry) we swallow
    the IntegrityError because the atomic UPDATE already happened and
    double-billing the same key would be worse than not re-logging.

    Returns a :class:`ConsumeResult`. See that class for field
    semantics.

    Callers should not catch exceptions from this function unless they
    have a principled reason — an error here usually means the DB is
    unreachable and the right response is a 500, not a silent allow.
    """
    if qty <= 0:
        raise ValueError(f"qty must be a positive int, got {qty!r}")

    result = await session.execute(
        _CONSUME_SQL,
        {
            "org_id": str(org_id),
            "feature": feature,
            "qty": qty,
            "now": _now_iso(),
        },
    )
    row = result.fetchone()
    if row is None:
        # UPDATE found no matching row. Either: (a) no entitlement for
        # this period (seeding bug or non-entitled feature), (b)
        # remaining < qty and overage disabled, or (c) period already
        # expired. We can't distinguish from the caller's side — the
        # result is the same: block.
        log.info(
            "try_consume: blocked org=%s feature=%s qty=%s",
            org_id,
            feature,
            qty,
        )
        return ConsumeResult(allowed=False, remaining=0, is_overage=False)

    new_remaining = int(row[0])
    # ``overage_enabled`` comes back as True / False / 1 / 0 depending
    # on the dialect. Normalise to Python bool for the dataclass.
    overage_enabled = bool(row[1])
    is_overage = new_remaining < 0 and overage_enabled

    if is_overage:
        # Log a billable usage event for Task 05 to report to Stripe.
        # We look up the tier for the cost lookup; if the org row is
        # gone (unusual), we store NULL cost and let support investigate.
        tier = await _resolve_tier_for_org(session, org_id)
        unit_cost = _lookup_overage_cost(tier, feature)
        total_cost: Optional[float] = (
            unit_cost * qty if unit_cost is not None else None
        )
        ukey = idempotency_key or f"overage_{uuid.uuid4().hex}"
        try:
            await session.execute(
                _INSERT_USAGE_EVENT_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "org_id": str(org_id),
                    "feature": feature,
                    "qty": qty,
                    "cost_usd": total_cost,
                    "idempotency_key": ukey,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            # An integrity error on the idempotency key means this
            # same logical consume has already been logged. The
            # UPDATE is already committed-in-progress; we do NOT
            # want to rollback and undo the consume. Log and carry on.
            log.warning(
                "try_consume: failed to log usage_event for org=%s feature=%s "
                "idem=%s: %s",
                org_id,
                feature,
                ukey,
                exc,
            )
        log.info(
            "try_consume: overage org=%s feature=%s qty=%s new_remaining=%s",
            org_id,
            feature,
            qty,
            new_remaining,
        )

    return ConsumeResult(
        allowed=True, remaining=new_remaining, is_overage=is_overage
    )


async def peek(
    session: AsyncSession, org_id: uuid.UUID, feature: str
) -> Optional[Dict[str, Any]]:
    """Read-only snapshot of the current period's entitlement.

    Never writes to the DB. Returns ``None`` if there is no matching
    row (feature not entitled, or period expired). Returns a dict with
    ``remaining``, ``overage_enabled``, ``included``, ``period_end``.
    """
    result = await session.execute(
        _PEEK_SQL,
        {"org_id": str(org_id), "feature": feature, "now": _now_iso()},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "remaining": int(row[0]),
        "overage_enabled": bool(row[1]),
        "included": int(row[2]),
        "period_end": row[3],
    }


# ---------------------------------------------------------------------------
# FastAPI decorator
# ---------------------------------------------------------------------------


def requires_entitlement(
    feature: str, consume: int = 1
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """FastAPI decorator that gates a route on a metered entitlement.

    Usage::

        @app.post("/api/ai/signal")
        @requires_entitlement("signals", consume=1)
        async def generate_signal(
            request: Request, response: Response, body: MyBody
        ):
            ...

    Contract with the wrapped route:

    * The route MUST accept ``request: Request`` and ``response:
      Response`` as keyword parameters. FastAPI will inject both. The
      decorator reads ``request.state.org_id`` (set by
      :class:`api.middleware.auth_stub.AuthStubMiddleware`) and writes
      the ``X-Entitlement-Remaining`` header to ``response``.

    * On quota exhaustion the decorator raises
      ``HTTPException(status_code=402, detail={...})`` *before* the
      wrapped function runs, so the route body never sees a blocked
      request.

    * The consume happens in a short-lived DB session opened by the
      decorator via :data:`_session_factory`. It is NOT wrapped around
      the route body — per the module docstring, the atomicity is in
      the SQL, not in transaction duration.

    Why not a FastAPI Depends?
        A Depends would work, but decorators make the gate obvious at
        the call site (``@requires_entitlement(...)`` right above the
        route def) and keep the route's own ``Depends`` list focused
        on actual data. We also avoid the footgun where a Depends
        forgets to be listed and silently de-gates the route.
    """

    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # FastAPI passes request/response as kwargs when they're
            # declared as typed route params. Pull them out by name.
            request: Optional[Request] = kwargs.get("request")
            response: Optional[Response] = kwargs.get("response")
            if request is None:
                raise RuntimeError(
                    "requires_entitlement requires the route to accept "
                    "`request: Request` as a keyword argument"
                )

            org_id = getattr(request.state, "org_id", None)
            if org_id is None:
                # No auth context → 401. Don't consume, don't leak the
                # existence of the feature.
                raise HTTPException(
                    status_code=401, detail="Authentication required"
                )

            async with _session_factory() as session:
                result = await try_consume(session, org_id, feature, consume)
                await session.commit()

            if not result.allowed:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "quota_exhausted",
                        "feature": feature,
                        "remaining": 0,
                    },
                )

            # Attach the diagnostic header. If the route didn't declare
            # ``response: Response`` this is a no-op — we prefer that
            # over raising, since the gate has already been applied.
            if response is not None:
                response.headers["X-Entitlement-Remaining"] = str(
                    result.remaining
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
