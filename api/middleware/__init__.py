"""HTTP middleware for the FastAPI control plane.

Two middlewares are shipped:

* :mod:`api.middleware.clerk_auth` — verifies Clerk-issued JWTs and
  resolves them to ``(user, org_id)`` on ``request.state``. JIT-
  provisions ``users`` / ``organizations`` rows on first sight. This
  is the production path (P0-2 in the Phase 2 go-live gap analysis).

* :mod:`api.middleware.auth_stub` — header-driven dev stub kept around
  for the existing test suite and local-only deployments.
  :class:`ClerkAuthMiddleware` falls back to the same headers when
  ``settings.clerk_issuer`` is empty, so the stub class is no longer
  mounted by default but the contract is preserved.
"""
from __future__ import annotations

from api.middleware.auth_stub import AuthStubMiddleware
from api.middleware.clerk_auth import ClerkAuthMiddleware, ClerkPrincipal

__all__ = ["AuthStubMiddleware", "ClerkAuthMiddleware", "ClerkPrincipal"]
