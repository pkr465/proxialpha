"""POST /agent/heartbeat — the agent's lifeline to the control plane.

This is the **single** endpoint the Customer Agent talks to during
normal operation. Every hour each agent does:

1. Builds a :class:`~api.agent.schemas.HeartbeatRequest` from its
   local state.
2. Sets ``Authorization: Bearer <current_license_jwt>``.
3. POSTs to ``/agent/heartbeat``.
4. On 200, swaps its license for the ``license`` field of the
   response and, if ``config_bundle`` is non-null, applies the new
   config on the next tick.
5. On 401/402/403/409, enters a local error-handling path per
   ADR-003 §7.

That means **this handler is load-bearing for every paying customer
running an on-prem agent**. The implementation below is deliberately
explicit about every failure case — generic 500s are never allowed
because the agent can't distinguish them from a transient network
blip, and misclassification can cost customers a full grace period.

High-level flow
---------------

::

    verify JWT                     → 401 invalid_token
    clock skew check               → 401 clock_skew
    fetch org subscription         → 403 license_revoked | 402 past_due
    fetch / create agents row      → 409 fingerprint_mismatch
    store metrics + heartbeat time
    compute grace_until slide rule
    diff org vs agent config_version
    assemble entitlements_snapshot
    issue new license JWT
    return 200 with license and config_bundle

Every branch logs at INFO or WARN. We never log the token itself —
only the first 12 chars of its fingerprint when relevant (sane
diagnostic, zero leak surface).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.agent.license_issuer import issue_license
from api.agent.schemas import HeartbeatRequest, HeartbeatResponse
from api.middleware.rate_limit import enforce_heartbeat_limit
from core import jwt_keys
from core.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Absolute clock-skew tolerance between the agent's reported ``now``
#: and the server's wall-clock, in seconds. 5 minutes matches the
#: JWT verification leeway — any stricter bound here would lead to
#: heartbeats rejected on skew that the token itself would accept,
#: which would be a confusing split rule.
CLOCK_SKEW_TOLERANCE_SECONDS = 300

#: Baseline agent grace window. When the org is healthy and the prior
#: grace_until is more than 6 days in the past, we refresh it to
#: ``now + 7 days``. If the agent keeps heartbeating hourly the
#: window never expires.
GRACE_WINDOW = timedelta(days=7)

#: Threshold for the slide rule. If the previous ``grace_until`` was
#: written MORE than this ago, it's treated as stale and refreshed
#: to ``now + GRACE_WINDOW``. Otherwise we preserve the existing
#: value so multiple heartbeats within a single day don't continuously
#: push the deadline forward (keeps test determinism simple too).
GRACE_SLIDE_THRESHOLD = timedelta(days=6)

#: Subscription statuses treated as "healthy enough to issue a license".
#: Anything else is handled explicitly in :func:`_classify_subscription`.
_HEALTHY_SUB_STATUSES = ("active", "trialing")


# ---------------------------------------------------------------------------
# Engine / session for the agent context
# ---------------------------------------------------------------------------

# Separate engine singleton from billing so the two contexts can be
# overridden independently in tests. Both ultimately point at the
# same DB in production.
_agent_engine = None
_agent_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_or_create_agent_engine() -> async_sessionmaker[AsyncSession]:
    """Lazily build the agent-context async engine from settings."""
    global _agent_engine, _agent_sessionmaker
    if _agent_engine is None:
        settings = get_settings()
        _agent_engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, pool_size=5
        )
        _agent_sessionmaker = async_sessionmaker(
            bind=_agent_engine, expire_on_commit=False, class_=AsyncSession
        )
    assert _agent_sessionmaker is not None
    return _agent_sessionmaker


async def _get_agent_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields an agent-endpoint session.

    Tests override this via ``app.dependency_overrides`` to swap in a
    SQLite-backed fixture session. Production uses the lazily-built
    async engine pointing at the main control-plane DB.
    """
    maker = _get_or_create_agent_engine()
    session = maker()
    try:
        yield session
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quick_agent_key(authorization: Optional[str]) -> Optional[str]:
    """Best-effort agent identifier extracted from a Bearer JWT.

    Used by the rate limiter (P1-8) to bucket requests per agent
    BEFORE we verify the token. We deliberately do not raise on a
    malformed input — the verifier below will produce the canonical
    rejection. The worst case for the limiter is "bucket key is
    None" which falls back to the per-IP bucket only.

    Why not reuse :func:`_parse_bearer`?
    Because the parser raises 401 on missing headers, and we don't
    want a missing-header request to skip the IP bucket entirely.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    token = parts[1]
    try:
        import jwt as _pyjwt

        unverified = _pyjwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
    sub = unverified.get("sub") if isinstance(unverified, dict) else None
    if not sub:
        return None
    return str(sub)


def _parse_bearer(authorization: Optional[str]) -> str:
    """Extract the JWT from an ``Authorization: Bearer <jwt>`` header.

    Raises a 401 ``invalid_token/malformed`` if the header is missing
    or doesn't start with the ``Bearer`` scheme. We never echo the
    token back in the error body.
    """
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_token",
                "reason": "malformed",
                "message": "missing Authorization header",
            },
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_token",
                "reason": "malformed",
                "message": "expected 'Bearer <token>' in Authorization header",
            },
        )
    return parts[1]


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise a possibly-naive datetime to tz-aware UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_db_datetime(value: Any) -> Optional[datetime]:
    """Coerce a DB column value into a tz-aware UTC ``datetime``.

    Postgres returns tz-aware datetimes directly; SQLite hands back
    ISO strings. We accept both so the handler runs unmodified under
    either dialect — the same pattern :mod:`jobs.meter_usage` uses.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            return _ensure_aware(datetime.fromisoformat(s))
        except ValueError:
            return None
    return None


def _now_utc(now: Optional[datetime] = None) -> datetime:
    """Return the handler's view of "now", always tz-aware UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    return _ensure_aware(now) or datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# DB access
# ---------------------------------------------------------------------------


async def _is_jti_revoked(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    jti: str,
    agent_id: str,
) -> Tuple[bool, Optional[str]]:
    """Return (revoked, reason) by consulting the ``revoked_jti`` table.

    Two revocation modes are supported (P1-7 in the gap analysis):

    * **Per-token** — an exact ``jti`` is on the list. Used by an
      operator to invalidate a single suspicious token without
      taking down the agent's underlying enrollment.
    * **Per-agent** — a row with ``jti = NULL`` (or the sentinel
      ``'*'``) and a matching ``agent_id``. Used to nuke a stolen
      agent identity entirely; every subsequent heartbeat from any
      token bound to that ``agent_id`` is rejected.

    The lookup is intentionally one round-trip and indexed on
    ``(org_id, jti)`` and ``(org_id, agent_id)`` (see migration 0004).
    A short row in this table is the cheapest way to express
    "block this thing now" without re-issuing the signing key.
    """
    result = await session.execute(
        text(
            """
            SELECT jti, agent_id, reason FROM revoked_jti
            WHERE org_id = :org
              AND (
                    jti = :jti
                 OR (jti IS NULL AND agent_id = :aid)
                 OR (jti = '*'   AND agent_id = :aid)
              )
            LIMIT 1
            """
        ),
        {"org": str(org_id), "jti": jti, "aid": str(agent_id)},
    )
    row = result.fetchone()
    if row is None:
        return False, None
    reason = row[2] if len(row) > 2 else None
    return True, (reason or "revoked")


async def _claim_jti(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    jti: str,
    agent_id: str,
    expires_at: datetime,
) -> bool:
    """Atomically claim a ``jti`` in the ``heartbeat_jti_seen`` table.

    Returns ``True`` on first sight, ``False`` if the row already
    existed (i.e. the heartbeat is a replay). The unique constraint
    on ``(org_id, jti)`` is the actual race-condition guard — we rely
    on the ``ON CONFLICT DO NOTHING`` returning zero affected rows.

    The ``expires_at`` column lets a periodic job prune entries past
    the JWT lifetime so the table doesn't grow unbounded. We use the
    license's ``exp`` claim plus a small skew margin so the row
    outlives any token that could legitimately replay it.
    """
    try:
        result = await session.execute(
            text(
                """
                INSERT INTO heartbeat_jti_seen
                    (org_id, jti, agent_id, seen_at, expires_at)
                VALUES (:org, :jti, :aid, :seen, :exp)
                ON CONFLICT (org_id, jti) DO NOTHING
                """
            ),
            {
                "org": str(org_id),
                "jti": jti,
                "aid": str(agent_id),
                "seen": datetime.now(timezone.utc).isoformat(),
                "exp": expires_at.isoformat(),
            },
        )
    except Exception as exc:  # pragma: no cover - DB schema mismatch
        log.warning("heartbeat: jti claim failed: %s", exc)
        # If the table is missing (test fixture without migration 0004)
        # we degrade to "replay protection unavailable" rather than
        # crashing the request. The DB-backed prod path always has it.
        return True

    rowcount = getattr(result, "rowcount", None)
    if rowcount is None:
        return True
    return rowcount > 0


async def _fetch_subscription(
    session: AsyncSession, org_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """Return the most recently-updated sub row for an org, or None."""
    result = await session.execute(
        text(
            "SELECT id, status, tier, current_period_end, cancel_at_period_end "
            "FROM subscriptions "
            "WHERE org_id = :org_id "
            "ORDER BY updated_at DESC "
            "LIMIT 1"
        ),
        {"org_id": str(org_id)},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "status": row[1],
        "tier": row[2],
        "current_period_end": _coerce_db_datetime(row[3]),
        "cancel_at_period_end": bool(row[4]),
    }


async def _fetch_agent_row(
    session: AsyncSession, org_id: uuid.UUID, agent_id: str
) -> Optional[Dict[str, Any]]:
    """Return the agents row for this (org, agent_id) or None."""
    result = await session.execute(
        text(
            "SELECT id, fingerprint, grace_until, config_version, mode "
            "FROM agents "
            "WHERE org_id = :org_id AND id = :id"
        ),
        {"org_id": str(org_id), "id": str(agent_id)},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "fingerprint": row[1],
        "grace_until": _coerce_db_datetime(row[2]),
        "config_version": int(row[3] or 0),
        "mode": row[4],
    }


async def _insert_new_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    org_id: uuid.UUID,
    fingerprint: str,
    version: Optional[str],
    hostname: Optional[str],
    topology: str,
    last_heartbeat_at: datetime,
    last_metrics: Dict[str, Any],
    grace_until: Optional[datetime],
) -> None:
    """Create the agents row for a first-heartbeat agent."""
    await session.execute(
        text(
            """
            INSERT INTO agents (
                id, org_id, fingerprint, hostname, version, topology,
                mode, last_heartbeat_at, last_metrics,
                grace_until, config_version
            )
            VALUES (
                :id, :org_id, :fp, :hostname, :version, :topology,
                'running', :last_hb, :metrics,
                :grace_until, 0
            )
            """
        ),
        {
            "id": str(agent_id),
            "org_id": str(org_id),
            "fp": fingerprint,
            "hostname": hostname,
            "version": version,
            "topology": topology,
            "last_hb": last_heartbeat_at.isoformat(),
            "metrics": _serialize_metrics(last_metrics),
            "grace_until": (
                grace_until.isoformat() if grace_until is not None else None
            ),
        },
    )


async def _update_existing_agent(
    session: AsyncSession,
    *,
    agent_id: str,
    org_id: uuid.UUID,
    version: Optional[str],
    hostname: Optional[str],
    last_heartbeat_at: datetime,
    last_metrics: Dict[str, Any],
    grace_until: Optional[datetime],
) -> None:
    """Update the agents row on a subsequent heartbeat."""
    await session.execute(
        text(
            """
            UPDATE agents SET
                hostname          = :hostname,
                version           = :version,
                mode              = 'running',
                last_heartbeat_at = :last_hb,
                last_metrics      = :metrics,
                grace_until       = :grace_until,
                updated_at        = :last_hb
            WHERE org_id = :org_id AND id = :id
            """
        ),
        {
            "id": str(agent_id),
            "org_id": str(org_id),
            "hostname": hostname,
            "version": version,
            "last_hb": last_heartbeat_at.isoformat(),
            "metrics": _serialize_metrics(last_metrics),
            "grace_until": (
                grace_until.isoformat() if grace_until is not None else None
            ),
        },
    )


def _serialize_metrics(metrics: Dict[str, Any]) -> str:
    """Serialise the metrics dict to JSON text.

    Agents table column is JSONB in prod, TEXT in SQLite tests — JSON
    text is the common denominator. We never store a Python dict
    directly because SQLite would reject it anyway.
    """
    import json

    return json.dumps(metrics or {}, default=str)


async def _fetch_org_config_version(
    session: AsyncSession, org_id: uuid.UUID
) -> int:
    """Return the org's current config_version, defaulting to 0."""
    result = await session.execute(
        text(
            "SELECT config_version FROM organizations WHERE id = :id"
        ),
        {"id": str(org_id)},
    )
    row = result.fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


async def _bump_agent_config_version(
    session: AsyncSession,
    *,
    agent_id: str,
    org_id: uuid.UUID,
    new_version: int,
) -> None:
    """Record that the agent has been told about the new config version."""
    await session.execute(
        text(
            "UPDATE agents SET config_version = :v WHERE org_id = :org AND id = :id"
        ),
        {"v": new_version, "org": str(org_id), "id": str(agent_id)},
    )


async def _build_entitlements_snapshot(
    session: AsyncSession,
    org_id: uuid.UUID,
    tier: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """Assemble the entitlements_snapshot for the JWT.

    Reads current entitlement rows (quota remaining per feature) for
    the org and folds them into a small dict the agent can query
    offline. Intentionally small and PII-free.
    """
    result = await session.execute(
        text(
            """
            SELECT feature, included, remaining, period_end, overage_enabled
            FROM entitlements
            WHERE org_id = :org_id
            """
        ),
        {"org_id": str(org_id)},
    )
    features: Dict[str, Any] = {}
    for row in result.fetchall():
        features[row[0]] = {
            "included": int(row[1] or 0),
            "remaining": int(row[2] or 0),
            "period_end": (
                _coerce_db_datetime(row[3]).isoformat()
                if _coerce_db_datetime(row[3]) is not None
                else None
            ),
            "overage_enabled": bool(row[4]),
        }
    return {
        "tier": tier or "free",
        "features": features,
        "generated_at": int(now.timestamp()),
    }


async def _build_config_bundle(
    session: AsyncSession, org_id: uuid.UUID, version: int
) -> Dict[str, Any]:
    """Return the full config bundle for an org at the current version.

    Phase 2 is deliberately simple: we return ``{"version": N}``.
    Phase 3 will expand this into feature-flag tables and per-tier
    policy knobs, but the handler interface stays the same.
    """
    return {
        "version": version,
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
    }


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _classify_subscription(
    sub: Optional[Dict[str, Any]],
) -> Tuple[str, Optional[str]]:
    """Return ``(disposition, reason)`` for a subscription row.

    Dispositions:
    * ``"healthy"`` — issue a fresh license
    * ``"past_due"`` — return 402 with grace_ends_at
    * ``"canceled"`` — return 403 license_revoked
    """
    if sub is None:
        # No subscription row at all → treat like canceled. The
        # agent should re-license via the activation flow.
        return "canceled", "no_subscription"
    status = sub.get("status")
    if status in _HEALTHY_SUB_STATUSES:
        return "healthy", None
    if status == "past_due":
        return "past_due", "past_due"
    # canceled / incomplete / incomplete_expired all map to revoked
    return "canceled", status or "unknown"


def _compute_new_grace_until(
    prev_grace: Optional[datetime], now: datetime
) -> datetime:
    """Apply the grace-slide rule.

    The spec text: *"refresh the grace_until to now + 7 days if the
    previous grace_until was more than 6 days in the past, otherwise
    preserve it"*.

    We also refresh unconditionally when ``prev_grace`` is ``None``
    (first-heartbeat or migrated-from-old-row case), because preserving
    ``None`` would give the agent a zero-grace window — not what anyone
    wants.

    Parameters
    ----------
    prev_grace
        The ``grace_until`` currently stored on the agents row.
    now
        Server wall-clock at request time.
    """
    if prev_grace is None:
        return now + GRACE_WINDOW
    age = now - prev_grace
    if age > GRACE_SLIDE_THRESHOLD:
        return now + GRACE_WINDOW
    return prev_grace


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    request: Request,
    authorization: Optional[str] = Header(default=None),
    session: AsyncSession = Depends(_get_agent_session),
) -> HeartbeatResponse:
    """Process an agent heartbeat and return a refreshed license.

    See module docstring for the full flow. Error codes are enumerated
    on :class:`api.agent.schemas.HeartbeatError`.
    """
    # The test hook: tests may attach a ``_heartbeat_now`` to the app
    # state to freeze server time. Production leaves it unset and we
    # call :func:`_now_utc` with ``None``.
    frozen_now = getattr(request.app.state, "_heartbeat_now", None) if request is not None else None
    now = _now_utc(frozen_now)

    # ----- 0. Rate limit (P1-8) -----
    # Per-IP and per-agent token-bucket. We extract a stable agent key
    # from the JWT WITHOUT verifying it (we just want a key, not a
    # claim) so the limiter trips before we burn DB queries on a
    # malformed flood. The verifier below still rejects bad tokens
    # with 401 — the limiter is purely a cost cap.
    agent_key = _quick_agent_key(authorization)
    enforce_heartbeat_limit(request, agent_key)

    # ----- 1. Verify token -----
    token = _parse_bearer(authorization)
    try:
        claims = jwt_keys.verify(token, now=now)
    except jwt_keys.InvalidToken as exc:
        log.info("heartbeat: token rejected reason=%s", exc.reason)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "reason": exc.reason},
        ) from exc

    # Pull the claims we need. Everything else in the JWT is
    # informational — we don't leak it back.
    try:
        org_id = uuid.UUID(str(claims["org_id"]))
        agent_id = str(claims["sub"])
        token_fingerprint = str(claims["agent_fingerprint"])
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("heartbeat: claim shape invalid: %s", exc)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "reason": "malformed"},
        ) from exc

    # The jti is required for replay protection (P1-6) but we treat it
    # as best-effort: licenses minted before the jti-everywhere change
    # may not have one, and we don't want to lock those agents out.
    # When present, both the revocation list (P1-7) and the seen-set
    # (P1-6) consult it.
    token_jti = str(claims["jti"]) if isinstance(claims.get("jti"), str) else None
    token_exp = claims.get("exp")
    try:
        token_exp_dt = (
            datetime.fromtimestamp(int(token_exp), tz=timezone.utc)
            if token_exp is not None
            else now + timedelta(hours=2)
        )
    except (ValueError, TypeError):
        token_exp_dt = now + timedelta(hours=2)

    # ----- 1b. Emergency revocation check (P1-7) -----
    # An operator can drop a row in ``revoked_jti`` to nuke a single
    # token (per-jti) or an entire agent identity (per-agent_id with
    # NULL/wildcard jti). We check this BEFORE the seen-set so a
    # revoked token never even pollutes the replay table.
    if token_jti is not None:
        try:
            revoked, reason = await _is_jti_revoked(
                session,
                org_id=org_id,
                jti=token_jti,
                agent_id=agent_id,
            )
        except Exception as exc:  # pragma: no cover - schema gap
            log.warning("heartbeat: revocation lookup failed: %s", exc)
            revoked, reason = False, None
        if revoked:
            log.info(
                "heartbeat: rejecting org=%s agent=%s license_revoked reason=%s",
                org_id,
                agent_id,
                reason,
            )
            raise HTTPException(
                status_code=403,
                detail={"error": "license_revoked", "reason": reason or "revoked"},
            )

    # ----- 1c. JTI replay protection (P1-6) -----
    # Atomically claim the jti in ``heartbeat_jti_seen``. A duplicate
    # row means an attacker is replaying an already-spent token; we
    # reject with 401 invalid_token/replayed and DO NOT issue a fresh
    # license. The seen-set is bounded by ``expires_at`` (set to the
    # token's exp + skew) so a periodic pruner can keep the table
    # small without weakening the rule for any live token.
    if token_jti is not None:
        first_sight = await _claim_jti(
            session,
            org_id=org_id,
            jti=token_jti,
            agent_id=agent_id,
            expires_at=token_exp_dt + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS),
        )
        if not first_sight:
            log.warning(
                "heartbeat: replay detected org=%s agent=%s jti=%s",
                org_id,
                agent_id,
                token_jti[:12],
            )
            raise HTTPException(
                status_code=401,
                detail={"error": "invalid_token", "reason": "replayed"},
            )

    # ----- 2. Clock-skew check -----
    agent_now = _ensure_aware(body.now)
    if agent_now is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "clock_skew", "reason": "missing_agent_now"},
        )
    skew = abs((agent_now - now).total_seconds())
    if skew > CLOCK_SKEW_TOLERANCE_SECONDS:
        log.info(
            "heartbeat: clock skew %.1fs exceeds tolerance for org=%s agent=%s",
            skew,
            org_id,
            agent_id,
        )
        raise HTTPException(
            status_code=401,
            detail={
                "error": "clock_skew",
                "reason": "clock_skew",
                "message": f"agent/server clock differ by {skew:.0f}s",
            },
        )

    # ----- 3. Subscription check -----
    sub = await _fetch_subscription(session, org_id)
    disposition, sub_reason = _classify_subscription(sub)

    if disposition == "canceled":
        log.info(
            "heartbeat: rejecting org=%s agent=%s license_revoked reason=%s",
            org_id,
            agent_id,
            sub_reason,
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "license_revoked", "reason": sub_reason},
        )

    if disposition == "past_due":
        # The 402 body carries the grace deadline so the agent can
        # display a countdown to the user without needing a second
        # API call. We use the current agent row's grace_until if it
        # exists; otherwise fall back to the subscription period end.
        existing = await _fetch_agent_row(session, org_id, agent_id)
        grace_ends_at = None
        if existing is not None and existing.get("grace_until") is not None:
            grace_ends_at = existing["grace_until"]
        elif sub is not None:
            grace_ends_at = sub.get("current_period_end")
        log.info(
            "heartbeat: org=%s past_due, grace_ends_at=%s", org_id, grace_ends_at
        )
        raise HTTPException(
            status_code=402,
            detail={
                "error": "past_due",
                "reason": "past_due",
                "grace_ends_at": (
                    grace_ends_at.isoformat() if grace_ends_at is not None else None
                ),
            },
        )

    # ----- 4. Fingerprint / first-heartbeat handling -----
    existing = await _fetch_agent_row(session, org_id, agent_id)
    if existing is not None:
        if existing["fingerprint"] != token_fingerprint:
            log.warning(
                "heartbeat: fingerprint mismatch for org=%s agent=%s "
                "stored=%s token=%s",
                org_id,
                agent_id,
                existing["fingerprint"][:12],
                token_fingerprint[:12],
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "fingerprint_mismatch",
                    "reason": "fingerprint_mismatch",
                },
            )
        prev_grace = existing.get("grace_until")
        agent_config_version = existing.get("config_version", 0)
        is_first_heartbeat = False
    else:
        prev_grace = None
        agent_config_version = 0
        is_first_heartbeat = True

    # ----- 5. Slide-rule grace + upsert agents row -----
    new_grace = _compute_new_grace_until(prev_grace, now)

    if is_first_heartbeat:
        await _insert_new_agent(
            session,
            agent_id=agent_id,
            org_id=org_id,
            fingerprint=token_fingerprint,
            version=body.version,
            hostname=body.hostname,
            topology=body.topology,
            last_heartbeat_at=now,
            last_metrics=body.metrics,
            grace_until=new_grace,
        )
    else:
        await _update_existing_agent(
            session,
            agent_id=agent_id,
            org_id=org_id,
            version=body.version,
            hostname=body.hostname,
            last_heartbeat_at=now,
            last_metrics=body.metrics,
            grace_until=new_grace,
        )

    # ----- 6. Config bundle delta -----
    org_config_version = await _fetch_org_config_version(session, org_id)
    config_bundle: Optional[Dict[str, Any]] = None
    if org_config_version > agent_config_version:
        config_bundle = await _build_config_bundle(
            session, org_id, org_config_version
        )
        await _bump_agent_config_version(
            session,
            agent_id=agent_id,
            org_id=org_id,
            new_version=org_config_version,
        )

    # ----- 7. Issue fresh license -----
    snapshot = await _build_entitlements_snapshot(
        session, org_id, (sub or {}).get("tier"), now
    )

    new_license = issue_license(
        org_id=org_id,
        agent_id=agent_id,
        agent_fingerprint=token_fingerprint,
        entitlements_snapshot=snapshot,
        grace_until=new_grace,
        now=now,
    )

    # Commit everything in one shot. We intentionally commit LAST so a
    # failure issuing the license doesn't leave a half-updated row.
    await session.commit()

    log.info(
        "heartbeat: ok org=%s agent=%s fp=%s bundle=%s",
        org_id,
        agent_id,
        token_fingerprint[:12],
        bool(config_bundle),
    )

    return HeartbeatResponse(
        license=new_license,
        config_bundle=config_bundle,
        rotate_token=True,
    )


__all__ = ["router", "heartbeat"]
