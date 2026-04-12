"""POST /agent/enroll — first-boot agent enrollment.

This is the route the Customer Agent calls **exactly once** at first
boot, before it has any signed license JWT in its possession. The
flow is the mirror image of :mod:`api.agent.heartbeat`:

1. Agent has a one-shot ``install_token`` (the admin generated it
   from the dashboard and pasted it into the agent's env). It also
   computes its stable ``fingerprint`` locally.
2. Agent POSTs ``{install_token, fingerprint}`` to ``/agent/enroll``.
3. Server validates the install token via
   :func:`api.agent.install_tokens.validate_and_consume_install_token`.
   That call atomically marks the token consumed so a replay loses.
4. Server upserts an ``agents`` row keyed on ``(org_id, fingerprint)``
   — re-enrolling the same fingerprint with a fresh install token is
   allowed and is how an admin "resets" a misbehaving agent.
5. Server issues an RS256-signed license JWT via
   :func:`api.agent.license_issuer.issue_license` with the same
   24-hour TTL the heartbeat handler uses.
6. Server returns ``{license, jwks_url, heartbeat_interval_seconds}``.

Why a separate file from :mod:`api.agent.heartbeat`?
----------------------------------------------------

The two endpoints have **completely different** auth models:

* ``/agent/enroll`` is unauthenticated by JWT — the install token IS
  the auth. There is no Authorization header.
* ``/agent/heartbeat`` requires a valid bearer JWT issued by us.

Mixing them in one router file would mean every reader has to track
two auth code paths in their head. Splitting them keeps each handler
focused. The combined router in :mod:`api.agent` mounts both under
``/agent``.

Failure modes
-------------

The handler is deliberately explicit about each rejection reason
because the agent's enrollment retry loop needs to distinguish
"transient — try again in 30s" from "fatal — bail and ask the human":

* 400 ``invalid_request`` — payload missing or shape-wrong. Fatal.
* 401 ``invalid_install_token`` reason ∈ {unknown, expired,
  consumed}. Fatal — the human must generate a fresh token.
* 500 ``internal_error`` — any unexpected failure. Transient — retry.

Security notes
--------------

* The install token plaintext is **never** logged. We log only the
  first 12 chars of the SHA-256 hash so an operator can correlate
  enroll attempts in the audit log without giving the log reader a
  bearer credential.
* The DB transaction wraps the install-token consume AND the agent
  upsert AND the audit-log row. A crash anywhere in between rolls
  back so we never end up with an "agent exists but token is still
  consumable" or vice-versa.
* Rate limiting on this endpoint is the responsibility of the
  middleware layer (P1-8) — keyed on client IP + install-token hash
  prefix. We don't enforce it inside the handler.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.heartbeat import _get_agent_session  # reuse the same session dep
from api.agent.install_tokens import (
    InvalidInstallToken,
    validate_and_consume_install_token,
)
from api.agent.license_issuer import DEFAULT_LICENSE_TTL, issue_license
from api.middleware.rate_limit import enforce_enroll_limit
from core import jwt_keys
from core.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter()


#: Default heartbeat cadence advertised to agents at enroll time.
#: Agents may receive a different value in a future config bundle but
#: this is the bootstrap floor. 1 hour matches ADR-003 §Heartbeat.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 3600


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    """The body of ``POST /agent/enroll``.

    Mirrors what :meth:`proxialpha_agent.license.LicenseClient.enroll`
    sends. Both fields are required — there is no useful default for
    either.
    """

    install_token: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="One-shot bearer string the admin pasted into the agent.",
    )
    fingerprint: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The agent's stable machine fingerprint (UUID4 hex).",
    )
    hostname: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Optional self-reported hostname for the dashboard.",
    )
    version: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional self-reported agent version string.",
    )
    topology: Optional[str] = Field(
        default="C",
        pattern="^[ABC]$",
        description="Deployment topology — A, B, or C per ADR-001.",
    )


class EnrollResponse(BaseModel):
    """Successful enroll response — what the agent persists on first boot.

    The ``jwks_url`` field is what closes P0-3: the agent caches it
    and falls back to fetching when its bundled key's ``kid`` doesn't
    match a future token's ``kid``. Setting it here at enroll time
    means even an agent with a stale build picks up the rotation
    story automatically.
    """

    license: str = Field(..., description="Signed RS256 license JWT.")
    jwks_url: Optional[str] = Field(
        default=None,
        description="Absolute URL of the control-plane JWKS endpoint.",
    )
    heartbeat_interval_seconds: int = Field(
        default=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        description="How often the agent should call /agent/heartbeat.",
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _find_agent_by_fingerprint(
    session: AsyncSession, *, org_id: uuid.UUID, fingerprint: str
) -> Optional[Dict[str, Any]]:
    """Return the agents row for ``(org_id, fingerprint)`` or None.

    The agents table has ``UNIQUE (org_id, fingerprint)`` so this is
    at most one row. Used to detect re-enrollment of an existing
    fingerprint, which is allowed and triggers an UPDATE rather than
    an INSERT.
    """
    result = await session.execute(
        text(
            "SELECT id, mode FROM agents "
            "WHERE org_id = :org AND fingerprint = :fp"
        ),
        {"org": str(org_id), "fp": fingerprint},
    )
    row = result.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "mode": row[1]}


async def _insert_enrollment_agent(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    org_id: uuid.UUID,
    fingerprint: str,
    hostname: Optional[str],
    version: Optional[str],
    topology: str,
    now: datetime,
) -> None:
    """Insert a fresh agents row for a brand-new enrollment.

    The agent's ``mode`` starts at ``booting``; the supervisor flips
    it to ``running`` on its first successful heartbeat. We do NOT
    set ``last_heartbeat_at`` here — the agent has not heart-beaten
    yet. Setting it would lie to the dashboard's "last seen" widget.
    """
    await session.execute(
        text(
            """
            INSERT INTO agents (
                id, org_id, fingerprint, hostname, version, topology,
                mode, created_at, updated_at, config_version
            )
            VALUES (
                :id, :org, :fp, :host, :ver, :topo,
                'booting', :ts, :ts, 0
            )
            """
        ),
        {
            "id": str(agent_id),
            "org": str(org_id),
            "fp": fingerprint,
            "host": hostname,
            "ver": version,
            "topo": topology,
            "ts": now.isoformat(),
        },
    )


async def _refresh_enrollment_agent(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    org_id: uuid.UUID,
    hostname: Optional[str],
    version: Optional[str],
    topology: str,
    now: datetime,
) -> None:
    """Re-enroll: bounce mode back to booting and refresh the metadata.

    The fingerprint is unchanged (it's the lookup key), so we leave
    it alone. We do reset ``mode`` to ``booting`` so an admin who
    just re-issued an install token can see the agent reboot.
    """
    await session.execute(
        text(
            """
            UPDATE agents SET
                hostname    = :host,
                version     = :ver,
                topology    = :topo,
                mode        = 'booting',
                updated_at  = :ts
            WHERE id = :id AND org_id = :org
            """
        ),
        {
            "id": str(agent_id),
            "org": str(org_id),
            "host": hostname,
            "ver": version,
            "topo": topology,
            "ts": now.isoformat(),
        },
    )


def _resolve_jwks_url() -> Optional[str]:
    """Compute the JWKS URL we should advertise to enrolling agents.

    Reads ``CONTROL_PLANE_PUBLIC_URL`` from settings; if not set we
    return None and the agent will fall back to its bundled public
    key. In production this MUST be set so key rotation works.
    """
    settings = get_settings()
    base = getattr(settings, "control_plane_public_url", None)
    if not base:
        return None
    return base.rstrip("/") + "/.well-known/jwks.json"


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    body: EnrollRequest,
    request: Request,
    session: AsyncSession = Depends(_get_agent_session),
) -> EnrollResponse:
    """First-boot agent enrollment.

    See module docstring for the full flow. This handler is the only
    one in the agent surface that does NOT require a JWT — the
    install token is the credential.
    """
    # P1-8: rate-limit BEFORE any DB work. The limiter is keyed on
    # client IP and a 12-char prefix of the install token, so a
    # brute-force attempt against random tokens spends both buckets
    # in seconds and gets shut out without ever consulting the DB.
    enforce_enroll_limit(request, body.install_token)

    now = datetime.now(timezone.utc)

    # ----- 1. Mint an agent_id we'll use for the consume call -----
    # We mint the UUID up front so we can pass it into
    # ``validate_and_consume_install_token`` (which records the
    # consuming agent in ``install_tokens.consumed_by_agent`` for
    # the audit trail). If the consume succeeds we then INSERT or
    # UPDATE the agents row using this id; if it fails we throw the
    # id away — no agents row is ever created.
    new_agent_id = uuid.uuid4()

    # ----- 2. Validate + atomically consume the install token -----
    try:
        validated = await validate_and_consume_install_token(
            session,
            plaintext=body.install_token,
            consumed_by_agent=new_agent_id,
            now=now,
        )
    except InvalidInstallToken as exc:
        # Roll back the failed consume attempt so the session is clean
        # for the next request handled on this connection.
        await session.rollback()
        log.info(
            "enroll: install token rejected reason=%s fp=%s",
            exc.reason,
            body.fingerprint[:12],
        )
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_install_token",
                "reason": exc.reason,
            },
        ) from exc

    org_id = validated.org_id

    # ----- 3. Upsert agents row by (org_id, fingerprint) -----
    # We checked AFTER the consume on purpose: a failed consume must
    # never reveal whether a fingerprint exists. The audit-clean
    # ordering is "consume → look → upsert".
    existing = await _find_agent_by_fingerprint(
        session, org_id=org_id, fingerprint=body.fingerprint
    )
    if existing is None:
        # Brand new agent.
        await _insert_enrollment_agent(
            session,
            agent_id=new_agent_id,
            org_id=org_id,
            fingerprint=body.fingerprint,
            hostname=body.hostname,
            version=body.version,
            topology=body.topology or "C",
            now=now,
        )
        agent_id = new_agent_id
    else:
        # Re-enroll path: keep the existing agent_id (so historical
        # heartbeat rows still join), refresh metadata, bounce mode.
        agent_id = uuid.UUID(existing["id"])
        await _refresh_enrollment_agent(
            session,
            agent_id=agent_id,
            org_id=org_id,
            hostname=body.hostname,
            version=body.version,
            topology=body.topology or "C",
            now=now,
        )
        # The consume call recorded ``consumed_by_agent = new_agent_id``
        # but we ended up reusing the existing row. Patch the audit
        # link so the install_tokens row points at the agent that
        # actually got the license.
        await session.execute(
            text(
                "UPDATE install_tokens SET consumed_by_agent = :a "
                "WHERE id = :t"
            ),
            {"a": str(agent_id), "t": str(validated.token_id)},
        )

    # ----- 4. Issue a fresh license JWT -----
    # Entitlements snapshot is empty at enroll time — the heartbeat
    # endpoint is what assembles a real one from the entitlements
    # table. The agent boots with no quota until its first heartbeat,
    # which is the conservative default we want.
    snapshot: Dict[str, Any] = {
        "tier": "unknown",
        "features": {},
        "generated_at": int(now.timestamp()),
    }
    license_jwt = issue_license(
        org_id=org_id,
        agent_id=str(agent_id),
        agent_fingerprint=body.fingerprint,
        entitlements_snapshot=snapshot,
        grace_until=None,
        now=now,
        ttl=DEFAULT_LICENSE_TTL,
    )

    # ----- 5. Commit everything together -----
    # The consume + agent upsert + audit patch all live in this
    # session's open transaction; commit them as one unit so a crash
    # anywhere above leaves nothing partially applied.
    await session.commit()

    log.info(
        "enroll: ok org=%s agent=%s fp=%s token=%s reenroll=%s",
        org_id,
        agent_id,
        body.fingerprint[:12],
        str(validated.token_id)[:8],
        existing is not None,
    )

    return EnrollResponse(
        license=license_jwt,
        jwks_url=_resolve_jwks_url(),
        heartbeat_interval_seconds=DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    )


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "EnrollRequest",
    "EnrollResponse",
    "enroll",
    "router",
]
