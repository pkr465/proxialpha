"""Pydantic request/response models for the agent heartbeat endpoint.

Used by :mod:`api.agent.heartbeat`. Kept in their own module so tests
can import the schemas without pulling the router (and with it FastAPI,
SQLAlchemy, PyJWT, etc.) into the import graph.

Design notes
------------

* The heartbeat payload is what the agent POSTs hourly. It mirrors
  ADR-003 §4 ("Heartbeat request shape") — **do not add PII here**.
  Specifically NOT allowed: Stripe customer IDs, email addresses,
  trade P&L, position quantities, price points. The control plane
  uses this to tell agents "you're still licensed" and nothing else.

* ``now`` is the agent's clock at the moment it generated the request.
  The handler compares this against server wall-clock with a 5-minute
  tolerance and rejects outside that window with a ``clock_skew`` 401.
  We intentionally keep this as a plain ``datetime`` rather than an
  integer — tz-aware datetimes serialise cleanly through pydantic v2
  and the +00:00 suffix makes log diagnostics trivial.

* ``metrics`` is a loose ``Dict[str, Any]`` because the agent is
  allowed to evolve its self-reported metrics without a control-plane
  schema change. The handler stores the whole dict verbatim in
  ``agents.last_metrics`` (JSONB) — it never *introspects* the fields.

* ``HeartbeatResponse.config_bundle`` is ``Optional[Dict[str, Any]]``:
  ``None`` when the agent's local config version already matches the
  org's current version, an opaque dict when a newer config should be
  applied. The handler never returns a partial diff — it's always the
  full bundle or nothing.

* ``rotate_token`` is always ``True`` in Phase 2. Every successful
  heartbeat returns a fresh license JWT and the agent unconditionally
  swaps it. Reserving the field (rather than omitting it) makes Phase
  3 "silent refresh on a subset of hits" a drop-in change.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class HeartbeatRequest(BaseModel):
    """Body for ``POST /agent/heartbeat``.

    The ``Authorization: Bearer <jwt>`` header carries the current
    agent license; this body carries the agent's self-reported state.
    Both must be present for the request to be accepted.
    """

    agent_id: str = Field(
        ...,
        description=(
            "Stable per-install agent identifier. Generated on first "
            "boot and reused across restarts; does NOT rotate when "
            "the license JWT rotates."
        ),
    )
    version: str = Field(
        ...,
        description="Agent binary version, e.g. '1.0.3'. Informational.",
    )
    topology: str = Field(
        ...,
        description="Deployment topology: 'A', 'B', or 'C' per ADR-003.",
    )
    hostname: Optional[str] = Field(
        default=None,
        description="Host the agent is running on. Informational.",
    )
    started_at: Optional[datetime] = Field(
        default=None,
        description="Process start time. Used to spot flapping agents.",
    )
    now: datetime = Field(
        ...,
        description=(
            "Agent's wall-clock at request send time (tz-aware). The "
            "server rejects requests more than 5 minutes off."
        ),
    )
    last_event_ts: Optional[datetime] = Field(
        default=None,
        description="Timestamp of the agent's most recent market event.",
    )
    metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Opaque self-reported metrics dict. Stored verbatim in "
            "agents.last_metrics — contents MUST NOT contain PII."
        ),
    )


class HeartbeatResponse(BaseModel):
    """Body returned on a successful ``POST /agent/heartbeat``.

    On a 200 the agent unconditionally swaps its in-memory license
    for the ``license`` field and, if ``config_bundle`` is not None,
    applies the new config on the next tick.
    """

    license: str = Field(
        ...,
        description=(
            "Fresh RS256-signed agent license JWT. Short-lived (24h). "
            "The agent should persist it and use it as the Bearer "
            "token for the next heartbeat."
        ),
    )
    config_bundle: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Full config bundle if the org's config_version has moved "
            "past the agent's, otherwise None. Never a partial diff."
        ),
    )
    rotate_token: bool = Field(
        default=True,
        description=(
            "Always True in Phase 2. Reserved so Phase 3 can add a "
            "'keep your current token' path without a schema break."
        ),
    )


class HeartbeatError(BaseModel):
    """Body returned on any non-2xx heartbeat response.

    The handler raises ``HTTPException(status_code, detail=<this>)``
    on each failure path; the ``error`` / ``reason`` pair drives
    agent-side retry / re-license flow logic.

    Error codes
    -----------

    * ``invalid_token`` (401) — JWT verification failed. ``reason``
      is one of ``expired``, ``signature``, ``issuer``, ``algorithm``,
      ``not_before``, ``malformed``.
    * ``clock_skew`` (401) — server/client clocks differ by > 5 min.
    * ``license_revoked`` (403) — org's subscription is canceled.
    * ``past_due`` (402) — org is in grace; ``grace_ends_at`` included.
    * ``fingerprint_mismatch`` (409) — agent's stored fingerprint does
      not match the JWT's ``agent_fingerprint`` claim.
    """

    error: str = Field(..., description="Short error code tag.")
    reason: Optional[str] = Field(
        default=None,
        description="Sub-code for multi-flavor errors like invalid_token.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Short human-readable hint. Never contains secrets.",
    )
    grace_ends_at: Optional[datetime] = Field(
        default=None,
        description="Set only on 402 past_due — when the grace window closes.",
    )
