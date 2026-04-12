"""Header-driven auth stub for Phase 1 development.

This middleware reads two request headers — ``X-Stub-User-Email`` and
``X-Stub-Org-Id`` — and stashes them on ``request.state`` so endpoint
handlers can read ``request.state.user`` and ``request.state.org_id``
without caring whether the auth came from a real Clerk JWT or a test.

TODO(phase1-task4): Replace this entire module with a real Clerk JWT
verifier. The replacement must:

* Read ``Authorization: Bearer <jwt>``.
* Verify the signature against Clerk's JWKS endpoint (cached).
* Extract ``sub`` (Clerk user ID) and the ``org_id`` claim.
* Look up our internal ``users`` and ``organizations`` rows by Clerk
  ID, creating them on first sight (Clerk JIT-provisioning).
* Set ``request.state.user`` and ``request.state.org_id`` to the same
  shape this stub uses, so endpoint code does not change.

Until that lands, every Phase 1 endpoint that needs auth uses this
stub. The stub is **never** mounted in production — :mod:`api.server`
will guard the install with an env check before Clerk goes live.

Why a middleware and not a dependency?
--------------------------------------

We considered making this a FastAPI ``Depends`` instead. We chose
middleware because:

1. It runs uniformly across the whole app — no per-route opt-in to
   forget.
2. It writes to ``request.state``, which downstream dependencies and
   endpoints can read without ordering concerns.
3. It mirrors how the real Clerk middleware will work, so the
   migration in Task 04 is "swap the class, keep the contract".

Endpoints that don't need auth (like ``/health``) simply don't read
from ``request.state.user``. The middleware never *rejects* a request
on missing headers — it just leaves ``state.user`` and ``state.org_id``
as ``None``. Auth enforcement happens in the endpoint, which can
return 401 if it needs an authed user. This keeps the middleware
generic and avoids hardcoding which paths are public.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StubUser:
    """Minimal user shape exposed on ``request.state.user``.

    The real Clerk-backed user object will be a strict superset of
    these fields, so endpoint code that only reads ``email`` will keep
    working unchanged after Task 04.
    """

    email: str


class AuthStubMiddleware(BaseHTTPMiddleware):
    """Read stub auth headers into ``request.state``.

    Headers:

    * ``X-Stub-User-Email`` → ``request.state.user`` (StubUser) or None
    * ``X-Stub-Org-Id``    → ``request.state.org_id`` (uuid.UUID) or None

    On a malformed UUID we log at WARNING and leave ``org_id`` as None
    so the downstream endpoint returns its own 401/400 — we never crash
    the request from middleware.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Default everyone to anonymous; endpoints that require auth
        # check for None and return 401 themselves.
        request.state.user = None
        request.state.org_id = None

        email = request.headers.get("x-stub-user-email")
        if email:
            request.state.user = StubUser(email=email)

        org_header = request.headers.get("x-stub-org-id")
        if org_header:
            try:
                request.state.org_id = uuid.UUID(org_header)
            except ValueError:
                log.warning(
                    "auth_stub: invalid X-Stub-Org-Id header value %r — ignoring",
                    org_header,
                )

        return await call_next(request)


def require_authed_org(request: Request) -> tuple[StubUser, uuid.UUID]:
    """Helper for endpoint handlers: return (user, org_id) or raise 401.

    Endpoints that need both an authed user and an org context call
    this at the top of the handler. Centralising the check here means
    Task 04's Clerk migration only has to update one function.

    Raises ``fastapi.HTTPException(401)`` if either is missing — the
    text is intentionally generic to avoid leaking which header was
    wrong (we don't want to teach attackers our header schema).
    """
    # Local import keeps starlette-only consumers from pulling fastapi.
    from fastapi import HTTPException

    user: Optional[StubUser] = getattr(request.state, "user", None)
    org_id: Optional[uuid.UUID] = getattr(request.state, "org_id", None)
    if user is None or org_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user, org_id
