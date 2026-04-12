"""``/.well-known/`` endpoints for the control plane.

Currently exposes exactly one route — ``GET /.well-known/jwks.json``
— which returns the JSON Web Key Set the agent uses to verify
license JWTs. This is the load-bearing piece for ADR-003's key
rotation story (see also ``docs/runbooks/signing-key-rotation.md``).

Why ``/.well-known/`` and not ``/api/jwks``?
--------------------------------------------

RFC 8615 defines ``/.well-known/`` as the standard prefix for
"discoverable" metadata endpoints — JWKS, OpenID config, OAuth
metadata, ACME challenges, etc. Agents that bootstrap against ANY
OpenID-compliant control plane will look here first, so colocating
the JWKS endpoint here keeps us spec-friendly and lets us drop the
agent's bundled URL config in a future refactor.

Cache headers
-------------

We set ``Cache-Control: public, max-age=600`` so a CDN or HTTP cache
in front of the control plane can absorb the JWKS fetch traffic. The
agent's own client adds a 10-minute in-process cache on top, so the
combined effect is "agents only fetch the JWKS once per rotation
window in steady state." During rotation the operator restarts the
API, the cache poisons clear, and agents pick up the new keys
within at most 20 minutes (CDN TTL + agent TTL).

No auth
-------

This endpoint is unauthenticated by design — any agent (or anyone
else) can fetch the public keys. That's the entire point of
asymmetric signing: knowing the public key gives no signing power.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core import jwt_keys

log = logging.getLogger(__name__)

router = APIRouter()


#: HTTP cache lifetime for the JWKS response in seconds. Matches the
#: agent-side cache TTL declared in :mod:`proxialpha_agent.license`
#: so the two layers don't fight over staleness.
JWKS_CACHE_SECONDS = 600


@router.get("/.well-known/jwks.json")
async def get_jwks() -> JSONResponse:
    """Return the public JWKS for the control plane's signing keys.

    Always returns 200 — even an empty key set is a valid response,
    though in practice :func:`core.jwt_keys.jwks` always includes at
    least the active key. We never raise from this handler; doing so
    would prevent agents from refreshing during a rotation incident,
    which is exactly when the JWKS endpoint matters most.
    """
    try:
        body: Dict[str, Any] = jwt_keys.jwks()
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("jwks: failed to build key set: %s", exc)
        # Empty key set is technically valid; agents will fall back
        # to their bundled key. Better than a 500 the agent treats
        # as a transient network issue.
        body = {"keys": []}

    return JSONResponse(
        content=body,
        headers={
            "Cache-Control": f"public, max-age={JWKS_CACHE_SECONDS}",
            "Content-Type": "application/jwk-set+json",
        },
    )


__all__ = ["JWKS_CACHE_SECONDS", "get_jwks", "router"]
