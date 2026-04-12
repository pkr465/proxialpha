"""Clerk JWT verification middleware (P0-2).

This module replaces :mod:`api.middleware.auth_stub` for production
deployments. The contract on ``request.state`` is identical — every
endpoint that uses :func:`require_authed_org` keeps working without
modification — but the source of truth for ``user`` and ``org_id`` is
now a verified Clerk-issued JWT instead of a developer-supplied header.

Migration story
---------------

We deliberately keep this middleware backwards-compatible with the
stub for two reasons:

1. ``settings.clerk_issuer`` defaults to the empty string. When empty,
   :class:`ClerkAuthMiddleware` falls back to reading the same
   ``X-Stub-User-Email`` / ``X-Stub-Org-Id`` headers that the stub
   used. Local dev and the existing test suite — which set those
   headers — keep working with zero changes.

2. When ``clerk_issuer`` is set, we **prefer** the Bearer token but
   still accept the stub headers if no token is present. That lets a
   single deployment carry both pre-Clerk and post-Clerk traffic
   during a phased rollout. If you want to lock the door entirely,
   set ``CLERK_REQUIRE_TOKEN=1`` and the stub fallback is disabled.

JIT provisioning
----------------

When a Clerk-verified JWT presents a ``sub`` (user id) and an
``org_id`` claim that we don't have rows for in our local
``users`` / ``organizations`` tables, we create them on the spot.
This matches Clerk's "source-of-truth" model — the dashboard never
pre-creates org rows on our side, and every authenticated request can
seed itself.

Provisioning is wrapped in an INSERT … ON CONFLICT DO NOTHING so two
concurrent first-requests for the same user or org cannot crash on a
duplicate-key race. The JIT path is gated by
``settings.clerk_jit_provision`` (default ``True``); operators who
prefer manual provisioning can set it to ``False`` and the verifier
will 403 unknown subjects.

JWKS caching
------------

Clerk publishes its public keys at ``${clerk_issuer}/.well-known/jwks.json``.
We cache the response in-process for ``settings.clerk_jwks_cache_seconds``
(default 600s = 10 min) — long enough to amortize the fetch across many
requests, short enough to pick up a key rotation within an SLA window.
The cache is per-process; horizontal-scaled deployments each fetch
once per TTL, which is well within Clerk's published rate limits.

We never raise from the JWKS fetcher's hot path. If the upstream is
down we return the previously-cached keys (even if stale) so a
transient Clerk outage doesn't take down our auth surface — the
worst case is a key-rotation during an outage, which is rare enough
to accept.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Re-export the existing dataclass and helper so callers can switch
# the import path without touching their endpoint code.
from api.middleware.auth_stub import StubUser, require_authed_org  # noqa: F401

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClerkPrincipal:
    """Verified Clerk principal exposed on ``request.state.user``.

    This is intentionally a strict superset of :class:`StubUser` — the
    ``email`` field has the same name and shape so endpoints that read
    ``request.state.user.email`` continue to work after the swap.
    """

    email: str
    clerk_user_id: str
    clerk_org_id: Optional[str]
    raw_claims: Dict[str, Any]


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------


class _JWKSCache:
    """Tiny in-process cache for Clerk's public keys.

    The cache is keyed on the issuer URL so that flipping
    ``clerk_issuer`` between dev and prod doesn't cross-contaminate.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def get(self, issuer: str, ttl: int) -> Optional[List[Dict[str, Any]]]:
        entry = self._entries.get(issuer)
        if entry is None:
            return None
        expires_at, keys = entry
        if time.time() >= expires_at:
            return None
        return keys

    def get_stale(self, issuer: str) -> Optional[List[Dict[str, Any]]]:
        """Return the cached keys regardless of TTL (used as a fallback)."""
        entry = self._entries.get(issuer)
        if entry is None:
            return None
        return entry[1]

    def set(self, issuer: str, keys: List[Dict[str, Any]], ttl: int) -> None:
        self._entries[issuer] = (time.time() + ttl, list(keys))

    def clear(self) -> None:
        self._entries.clear()


_jwks_cache = _JWKSCache()


def _reset_jwks_cache_for_tests() -> None:
    """Test hook — clear the module-level JWKS cache."""
    _jwks_cache.clear()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ClerkAuthMiddleware(BaseHTTPMiddleware):
    """Verify a Clerk Bearer JWT and stash the principal on ``request.state``.

    The middleware NEVER raises from ``dispatch``. On any failure mode
    (no token, bad token, expired token, JWKS fetch failure) it leaves
    ``request.state.user`` and ``request.state.org_id`` as ``None``
    and lets the endpoint return its own 401 via
    :func:`require_authed_org`. This keeps the middleware generic
    across public and private routes.

    Behaviour matrix
    ----------------

    +---------------------+---------------------+----------------------+
    | clerk_issuer set?   | Bearer token sent?  | Resulting state.user |
    +=====================+=====================+======================+
    | no                  | no                  | from stub headers    |
    | no                  | yes                 | from stub headers    |
    | yes                 | no                  | from stub headers    |
    | yes                 | yes (valid)         | ClerkPrincipal       |
    | yes                 | yes (invalid)       | None                 |
    +---------------------+---------------------+----------------------+

    Setting ``clerk_require_token=True`` removes the stub fallback —
    the middleware will refuse to populate ``state`` from headers when
    Clerk is configured. Recommended for production.
    """

    def __init__(self, app, *, http_get: Optional[Callable[..., Any]] = None) -> None:
        super().__init__(app)
        self._http_get = http_get  # injected in tests

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request.state.user = None
        request.state.org_id = None

        try:
            await self._populate_state(request)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "clerk_auth: unexpected error during dispatch (path=%s): %s",
                request.url.path,
                exc,
            )

        return await call_next(request)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _populate_state(self, request: Request) -> None:
        settings = self._settings()
        issuer = (settings.clerk_issuer or "").rstrip("/")
        require_token = bool(getattr(settings, "clerk_require_token", False))

        bearer = self._extract_bearer(request)
        if issuer and bearer:
            principal = await self._verify_bearer(bearer, issuer, settings)
            if principal is not None:
                request.state.user = principal
                if principal.clerk_org_id:
                    org_uuid = await self._resolve_org_uuid(
                        principal.clerk_org_id, principal, settings
                    )
                    if org_uuid is not None:
                        request.state.org_id = org_uuid
                return

        if require_token:
            # Production lockdown — no stub fallback at all.
            return

        # Stub fallback — local dev and existing test suite.
        self._populate_from_stub_headers(request)

    @staticmethod
    def _settings() -> Any:
        from core.settings import get_settings

        return get_settings()

    @staticmethod
    def _extract_bearer(request: Request) -> Optional[str]:
        auth = request.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        token = auth.split(" ", 1)[1].strip()
        return token or None

    @staticmethod
    def _populate_from_stub_headers(request: Request) -> None:
        email = request.headers.get("x-stub-user-email")
        if email:
            request.state.user = StubUser(email=email)

        org_header = request.headers.get("x-stub-org-id")
        if org_header:
            try:
                request.state.org_id = uuid.UUID(org_header)
            except ValueError:
                log.warning(
                    "clerk_auth: invalid X-Stub-Org-Id header value %r — ignoring",
                    org_header,
                )

    # ------------------------------------------------------------------
    # JWT verification
    # ------------------------------------------------------------------

    async def _verify_bearer(
        self, token: str, issuer: str, settings: Any
    ) -> Optional[ClerkPrincipal]:
        """Verify ``token`` against Clerk's JWKS.

        Returns the parsed principal on success or ``None`` on any
        failure. Failures are logged at INFO level — we deliberately
        do not crash, log at ERROR, or expose decode internals so an
        attacker can't pivot off log noise to refine forgery attempts.
        """
        try:
            import jwt as _pyjwt
        except ImportError:
            log.warning(
                "clerk_auth: PyJWT not installed; cannot verify token. "
                "Install pyjwt[crypto] to enable Clerk auth."
            )
            return None

        try:
            unverified = _pyjwt.get_unverified_header(token)
        except Exception as exc:
            log.info("clerk_auth: malformed token header: %s", exc)
            return None
        kid = unverified.get("kid") if isinstance(unverified, dict) else None

        public_pem = await self._public_key_for(issuer, kid, settings)
        if public_pem is None:
            log.info("clerk_auth: no public key for kid=%r", kid)
            return None

        try:
            claims = _pyjwt.decode(
                token,
                public_pem,
                algorithms=["RS256"],
                issuer=issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except Exception as exc:
            log.info("clerk_auth: token rejected: %s", exc.__class__.__name__)
            return None

        sub = claims.get("sub")
        if not sub:
            log.info("clerk_auth: token has no sub claim")
            return None

        # Clerk emits the org id under one of several claim names
        # depending on the JWT template. We accept the common ones.
        org_id = (
            claims.get("org_id")
            or claims.get("o", {}).get("id") if isinstance(claims.get("o"), dict) else None
        )
        if org_id is None:
            org_id = claims.get("org_id")
        email = claims.get("email") or claims.get("primary_email") or ""

        return ClerkPrincipal(
            email=email,
            clerk_user_id=str(sub),
            clerk_org_id=str(org_id) if org_id else None,
            raw_claims=claims,
        )

    async def _public_key_for(
        self, issuer: str, kid: Optional[str], settings: Any
    ) -> Optional[bytes]:
        ttl = int(getattr(settings, "clerk_jwks_cache_seconds", 600) or 600)
        keys = _jwks_cache.get(issuer, ttl)
        if keys is None:
            try:
                keys = await self._fetch_jwks(issuer)
                _jwks_cache.set(issuer, keys, ttl)
            except Exception as exc:
                log.warning("clerk_auth: JWKS fetch failed for %s: %s", issuer, exc)
                # Stale cache is better than no cache.
                keys = _jwks_cache.get_stale(issuer)
                if keys is None:
                    return None

        for jwk in keys:
            if kid is None or jwk.get("kid") == kid:
                pem = self._jwk_to_pem(jwk)
                if pem is not None:
                    return pem
        return None

    async def _fetch_jwks(self, issuer: str) -> List[Dict[str, Any]]:
        url = f"{issuer}/.well-known/jwks.json"
        if self._http_get is not None:
            data = await _maybe_await(self._http_get(url))
        else:
            data = await self._default_http_get(url)
        if isinstance(data, (bytes, str)):
            data = json.loads(data)
        keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(keys, list):
            return []
        return keys

    @staticmethod
    async def _default_http_get(url: str) -> Dict[str, Any]:
        # Local import so a missing httpx in trading-only checkouts
        # can't poison the import graph.
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _jwk_to_pem(jwk: Dict[str, Any]) -> Optional[bytes]:
        """Convert a single RSA JWK to a PEM-encoded public key."""
        if jwk.get("kty") != "RSA":
            return None
        try:
            import base64

            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            return None

        def _b64u_to_int(value: str) -> int:
            pad = "=" * (-len(value) % 4)
            return int.from_bytes(base64.urlsafe_b64decode(value + pad), "big")

        try:
            n = _b64u_to_int(jwk["n"])
            e = _b64u_to_int(jwk["e"])
        except (KeyError, ValueError):
            return None

        public_numbers = rsa.RSAPublicNumbers(e=e, n=n)
        public_key = public_numbers.public_key()
        return public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    # ------------------------------------------------------------------
    # JIT provisioning
    # ------------------------------------------------------------------

    async def _resolve_org_uuid(
        self,
        clerk_org_id: str,
        principal: ClerkPrincipal,
        settings: Any,
    ) -> Optional[uuid.UUID]:
        """Look up our internal ``organizations.id`` for ``clerk_org_id``.

        If the row doesn't exist and ``clerk_jit_provision`` is on, we
        create both the org row and the user row in a single short-
        lived transaction. The provisioning path swallows DB errors
        and returns ``None`` so middleware never crashes a request —
        the endpoint will then return its own 401 from
        :func:`require_authed_org`.
        """
        try:
            from sqlalchemy import text

            from core.db import get_session  # type: ignore
        except Exception:
            return None

        try:
            async with get_session() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT id FROM organizations "
                            "WHERE clerk_org_id = :coi LIMIT 1"
                        ),
                        {"coi": clerk_org_id},
                    )
                ).fetchone()
                if row is not None:
                    org_uuid = uuid.UUID(str(row[0]))
                    await self._ensure_user(session, org_uuid, principal)
                    await session.commit()
                    return org_uuid

                if not getattr(settings, "clerk_jit_provision", True):
                    return None

                org_uuid = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO organizations (id, name, clerk_org_id) "
                        "VALUES (:id, :name, :coi) "
                        "ON CONFLICT (clerk_org_id) DO NOTHING"
                    ),
                    {
                        "id": str(org_uuid),
                        "name": principal.raw_claims.get("org_name")
                        or f"clerk:{clerk_org_id}",
                        "coi": clerk_org_id,
                    },
                )
                # Re-fetch in case another worker won the race.
                row = (
                    await session.execute(
                        text(
                            "SELECT id FROM organizations "
                            "WHERE clerk_org_id = :coi LIMIT 1"
                        ),
                        {"coi": clerk_org_id},
                    )
                ).fetchone()
                if row is None:
                    await session.rollback()
                    return None
                org_uuid = uuid.UUID(str(row[0]))
                await self._ensure_user(session, org_uuid, principal)
                await session.commit()
                return org_uuid
        except Exception as exc:
            log.warning(
                "clerk_auth: JIT provisioning failed for clerk_org_id=%s: %s",
                clerk_org_id,
                exc,
            )
            return None

    @staticmethod
    async def _ensure_user(session: Any, org_uuid: uuid.UUID, principal: ClerkPrincipal) -> None:
        from sqlalchemy import text

        if not principal.email or not principal.clerk_user_id:
            return
        await session.execute(
            text(
                "INSERT INTO users (id, email, org_id, clerk_user_id) "
                "VALUES (gen_random_uuid(), :email, :org, :cuid) "
                "ON CONFLICT (clerk_user_id) DO UPDATE SET org_id = EXCLUDED.org_id"
            ),
            {
                "email": principal.email,
                "org": str(org_uuid),
                "cuid": principal.clerk_user_id,
            },
        )


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it's awaitable, else return as-is."""
    if hasattr(value, "__await__"):
        return await value
    return value


__all__ = [
    "ClerkAuthMiddleware",
    "ClerkPrincipal",
    "_reset_jwks_cache_for_tests",
]
