"""Issue agent license JWTs.

Thin wrapper over :func:`core.jwt_keys.sign`. Exists so the heartbeat
handler doesn't have to know the shape of the claims dict — it just
calls :func:`issue_license` with the high-level fields and this
module stamps them into the correct JWT payload.

Claims layout (ADR-003 §5)
--------------------------

::

    {
        "iss":  "proxialpha-control-plane",   # stamped by jwt_keys.sign
        "sub":  "<agent_id>",                  # the agent's stable id
        "org_id": "<uuid>",                    # owning organization
        "agent_fingerprint": "<sha256 hex>",   # host/machine binding
        "entitlements_snapshot": {
            "tier": "pro",
            "features": {"signals": {...}, ...},
            "generated_at": <epoch int>
        },
        "grace_until": <epoch int or null>,    # past_due grace deadline
        "iat":  <epoch>,
        "nbf":  <epoch>,
        "exp":  <iat + 24h>,
    }

The ``entitlements_snapshot`` block is a copy-at-issue of the org's
current billing state. The agent uses it to gate local features
between heartbeats — so even if the control plane is unreachable,
the agent can still answer "am I entitled to signals?" offline up
until the token expires.

PII rule
--------

Per the Task 06 spec "Do not" list, these fields are **forbidden**
in the claims dict:

* email addresses
* Stripe customer IDs (``cus_*``)
* raw price IDs
* user display names
* hostnames (the agent has these locally; we don't need to tell
  it what it already knows)

:func:`issue_license` is the funnel — if a future refactor tries to
add one of these, the linting layer should catch it at review time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional
from uuid import UUID

from core import jwt_keys

#: Default lifetime for an agent license JWT. Agents heartbeat hourly
#: so 24h gives 24 chances to refresh before the token becomes
#: invalid — well inside the grace window the agent falls back to on
#: repeated heartbeat failures.
DEFAULT_LICENSE_TTL = timedelta(hours=24)


def issue_license(
    *,
    org_id: UUID | str,
    agent_id: str,
    agent_fingerprint: str,
    entitlements_snapshot: Mapping[str, Any],
    grace_until: Optional[datetime] = None,
    now: Optional[datetime] = None,
    ttl: timedelta = DEFAULT_LICENSE_TTL,
) -> str:
    """Build and sign an agent license JWT.

    Parameters
    ----------
    org_id
        UUID of the owning organization. Serialised to string so the
        JSON payload is trivially comparable.
    agent_id
        The agent's stable ``agent_id`` — stamped as the ``sub`` claim.
    agent_fingerprint
        SHA-256 hex digest of the machine/install fingerprint. The
        agent re-verifies this on boot to detect tampering.
    entitlements_snapshot
        Copy-at-issue of the org's current entitlement state. The
        caller is responsible for assembling this from the DB.
    grace_until
        ``datetime`` when the past-due grace window closes, or None
        if the org is in good standing. Serialised as an epoch int.
    now
        Override for "current time" used in ``iat``/``nbf``/``exp``.
        Tests pass a fixed datetime; production omits this.
    ttl
        How long the token is valid. Defaults to 24h per ADR-003.

    Returns
    -------
    str
        Compact JWT ready to return in the heartbeat response body.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    claims: Dict[str, Any] = {
        "sub": str(agent_id),
        "org_id": str(org_id),
        "agent_fingerprint": agent_fingerprint,
        "entitlements_snapshot": dict(entitlements_snapshot),
    }

    if grace_until is not None:
        if grace_until.tzinfo is None:
            grace_until = grace_until.replace(tzinfo=timezone.utc)
        claims["grace_until"] = int(grace_until.timestamp())
    else:
        claims["grace_until"] = None

    return jwt_keys.sign(claims, expires_in=ttl, now=stamp)


__all__ = ["DEFAULT_LICENSE_TTL", "issue_license"]
