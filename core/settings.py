"""Application settings for the ProxiAlpha control plane.

This module is the single source of truth for runtime configuration. It
uses ``pydantic-settings`` so values flow from environment variables (and
optionally a ``.env`` file in the repo root) with type validation.

Task 01 only needs ``database_url``, but this file is structured so future
tasks (Stripe, Clerk, LLM gateway) can add fields here without touching
the existing call sites.

We deliberately do **not** touch ``core/config.py`` (the legacy constants
file used by the trading engines) — that file continues to exist and is
unrelated to the control-plane schema.
"""
from __future__ import annotations

from functools import lru_cache

try:
    # pydantic-settings is a separate package in Pydantic v2.
    from pydantic import Field
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as exc:  # pragma: no cover - surfaced at import time
    raise ImportError(
        "pydantic-settings is required. Install with: "
        "pip install 'pydantic-settings>=2.0'"
    ) from exc


class Settings(BaseSettings):
    """Typed view of the process environment.

    All fields can be overridden with environment variables of the same
    name in upper case. The ``.env`` file is read if present but is never
    committed (see ``.gitignore``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Ignore unknown env vars so the trading engines' existing env can
        # coexist without Settings() exploding at import time.
        extra="ignore",
    )

    # -----------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------
    # Must use the ``postgresql+asyncpg://`` driver prefix for async
    # SQLAlchemy / Alembic. Example for local dev:
    #   postgresql+asyncpg://postgres:postgres@localhost:5432/proxialpha
    database_url: str = Field(
        default="postgresql+asyncpg://localhost/proxialpha_dev",
        description="Async SQLAlchemy URL for the control-plane Postgres DB.",
    )

    # -----------------------------------------------------------------
    # Stripe (Task 02 — billing webhook handler)
    # -----------------------------------------------------------------
    # Secret key for server-side Stripe API calls. Task 02's webhook
    # handler is a pure *consumer* (verifies signatures, never calls
    # Stripe), so this field is defined here but only used by Task 03's
    # Checkout/Portal endpoints. Must start with ``sk_test_`` or
    # ``sk_live_`` — we intentionally do not validate that here so tests
    # can pass obvious fake values.
    stripe_secret_key: str = Field(
        default="sk_test_placeholder",
        description="Stripe server-side secret key (sk_test_... or sk_live_...).",
    )

    # Webhook signing secret from the Stripe dashboard (whsec_...).
    # Used by ``api.billing.webhook`` to call
    # ``stripe.Webhook.construct_event(payload, sig_header, secret)``.
    # Rotate by generating a new secret in the dashboard and updating
    # this env var — no code change required.
    stripe_webhook_secret: str = Field(
        default="whsec_test_placeholder",
        description="Stripe webhook endpoint signing secret (whsec_...).",
    )

    # -----------------------------------------------------------------
    # Application URLs (Task 03 — Checkout/Portal endpoints)
    # -----------------------------------------------------------------
    # Base URL of the user-facing dashboard. The Customer Portal endpoint
    # uses this to build a ``return_url`` so that customers land back on
    # the dashboard after editing their payment method or cancelling.
    # In dev: http://localhost:3000. In prod: https://app.proxiant.io.
    # No trailing slash — the endpoint appends the path itself.
    app_url: str = Field(
        default="http://localhost:3000",
        description="Base URL of the dashboard frontend (no trailing slash).",
    )

    # -----------------------------------------------------------------
    # Control plane self-URL (Phase 2 — agent enroll / JWKS)
    # -----------------------------------------------------------------
    # Public-facing base URL of the control plane itself, advertised to
    # agents at enroll time so they can fetch ``/.well-known/jwks.json``
    # for key rotation. In dev this defaults to localhost; in prod it
    # MUST be set so agents can resolve the JWKS endpoint after a key
    # rotation. No trailing slash — the JWKS resolver appends the path.
    control_plane_public_url: str = Field(
        default="http://localhost:8000",
        description=(
            "Base URL of the control plane (no trailing slash). "
            "Used to advertise /.well-known/jwks.json to enrolling agents."
        ),
    )

    # -----------------------------------------------------------------
    # CORS (P1-1)
    # -----------------------------------------------------------------
    # Comma-separated list of origins allowed to make credentialed
    # requests to the API. Default is the local dashboard only — prod
    # MUST set this explicitly. Empty string disables CORS entirely.
    # We never want ``["*"]`` here: combined with credentialed requests
    # that's a self-serve tenant dump.
    cors_allowed_origins: str = Field(
        default="http://localhost:3000",
        description="Comma-separated list of CORS allow-listed origins.",
    )

    # -----------------------------------------------------------------
    # Clerk auth (P0-2)
    # -----------------------------------------------------------------
    # Clerk JWT issuer URL — looks like
    # ``https://<your-instance>.clerk.accounts.dev`` or
    # ``https://clerk.<yourdomain>.com``. The verifier fetches
    # ``${clerk_issuer}/.well-known/jwks.json`` to get the public keys.
    clerk_issuer: str = Field(
        default="",
        description="Clerk JWT issuer base URL (empty disables real auth).",
    )
    # JWKS cache TTL in seconds. 600s (10m) matches Clerk's recommended
    # cache window — long enough to amortize the fetch, short enough to
    # pick up a key rotation within an SLA window.
    clerk_jwks_cache_seconds: int = Field(
        default=600,
        description="How long to cache Clerk JWKS responses in seconds.",
    )
    # If true, the auth dependency will JIT-create an organizations row
    # the first time it sees a Clerk org_id it doesn't have locally. We
    # default to on because the dashboard never PRE-creates rows on our
    # side — Clerk is the source of truth for org existence.
    clerk_jit_provision: bool = Field(
        default=True,
        description="JIT-create organizations/users rows on first Clerk sight.",
    )
    # If true, the auth middleware refuses to fall back to the legacy
    # ``X-Stub-*`` headers when Clerk is configured. Recommended for
    # production deployments — locks the door so a forgotten test
    # header path can't be used as a tenant-spoof bypass.
    clerk_require_token: bool = Field(
        default=False,
        description="Disable the X-Stub-* header fallback when Clerk is on.",
    )

    # -----------------------------------------------------------------
    # Entitlements gate (P1-2)
    # -----------------------------------------------------------------
    # Whether the entitlements middleware enforces tier checks. Defaults
    # to ON in production. Local development can opt out by setting
    # ``ENTITLEMENTS_ENABLED=0`` — the env var name is preserved for
    # backwards compat with existing dev setups, but the DEFAULT is now
    # the safe one.
    entitlements_enabled: bool = Field(
        default=True,
        alias="ENTITLEMENTS_ENABLED",
        description="Master switch for the entitlements gate (default ON).",
    )

    # -----------------------------------------------------------------
    # Signing key location (P1-5)
    # -----------------------------------------------------------------
    # Where to load the RS256 agent signing key from. The current
    # implementation reads either a PEM file path or a literal PEM. The
    # ``signing_key_provider`` flag is forward-compat for KMS / Vault:
    # ``"file"`` (default), ``"aws-kms"``, ``"gcp-kms"``, ``"vault"``.
    # The non-file providers are not implemented yet — see
    # ``docs/runbooks/signing-key-rotation.md``.
    signing_key_provider: str = Field(
        default="file",
        description="Where the RS256 signing key comes from (file/aws-kms/...).",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide singleton ``Settings`` instance.

    Cached so repeated calls are free; call ``get_settings.cache_clear()``
    in tests if you need to re-read the environment.
    """
    return Settings()
