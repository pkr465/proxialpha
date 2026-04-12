"""Agent control-plane subpackage — Phase 2 (Customer Agent).

This package houses the HTTP surface that the on-premises Customer
Agent uses to talk to the control plane. For Phase 2 there are
exactly two endpoints:

* ``POST /agent/enroll``    — first-boot, install-token authed
* ``POST /agent/heartbeat`` — every hour, license-JWT authed

plus the license-issuance and JWT-verification plumbing that
supports both.

Layout
------

* :mod:`api.agent.schemas` — Pydantic request/response models for
  the heartbeat endpoint. Kept separate so tests can import them
  without pulling in the router (and thus the whole FastAPI dep
  stack).
* :mod:`api.agent.license_issuer` — thin wrapper over
  :mod:`core.jwt_keys` that stamps the agent-license claims
  (``org_id``, ``agent_id``, ``agent_fingerprint``,
  ``entitlements_snapshot``, ``grace_until``). Never called directly
  by HTTP code — always goes through :func:`issue_license`.
* :mod:`api.agent.install_tokens` — issuance + atomic-consume
  primitives for the one-shot bearer strings the admin generates
  from the dashboard. Used by both the enroll route (consumer) and
  the dashboard route (issuer).
* :mod:`api.agent.enroll` — the FastAPI router implementing
  ``POST /agent/enroll``. Handles first-boot enrollment via install
  token; the only handler in the agent surface that does NOT take
  an Authorization header.
* :mod:`api.agent.heartbeat` — the FastAPI router implementing
  ``POST /agent/heartbeat``. All DB work, token verification,
  fingerprint checking, grace-window logic, and config-bundle
  diffing live here.

The :data:`agent_router` re-export is what :mod:`api.server` mounts
under the ``/agent`` prefix — a single symbol so the server module
doesn't need to know the internal file layout.
"""
from __future__ import annotations

from fastapi import APIRouter

from api.agent.enroll import router as _enroll_router
from api.agent.heartbeat import router as _heartbeat_router

#: Combined router for the ``/agent`` mount point. Order doesn't
#: matter for routing (the paths don't overlap) but we list enroll
#: first because it's chronologically the first endpoint any agent
#: hits — readers grepping this file see the boot flow in order.
agent_router = APIRouter()
agent_router.include_router(_enroll_router)
agent_router.include_router(_heartbeat_router)

__all__ = ["agent_router"]
