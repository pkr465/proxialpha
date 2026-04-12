"""Environment-driven configuration for the ProxiAlpha Customer Agent.

All runtime knobs live here so the rest of the agent code never
touches ``os.environ`` directly. Fields are typed with
``pydantic-settings`` so bad values surface at boot, not halfway
through the first heartbeat loop.

Env var precedence
------------------

``pydantic-settings`` reads values in this order (highest priority first):

1. Values passed to :func:`AgentSettings` at construction time (tests).
2. Environment variables with the exact ``PROXIALPHA_*`` prefix.
3. ``.env`` file in the process CWD (dev only — not used in prod).
4. Field defaults defined below.

The prefix ``PROXIALPHA_`` isolates the agent's env from the
trading engine's legacy env (which uses no prefix). A customer who
already has ``DATABASE_URL`` pointing at their own local SQLite
will not see it picked up as the control-plane URL.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from pydantic import Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pydantic-settings is required. Install: pip install 'pydantic-settings>=2.0'"
    ) from exc


#: Default data directory when ``PROXIALPHA_HOME`` is unset.
#:
#: On Linux we use ``/var/lib/proxialpha``. On macOS the installer
#: places it under ``~/Library/Application Support/ProxiAlpha``
#: and on Windows under ``%LOCALAPPDATA%\ProxiAlpha``. Those are
#: chosen by the installer script; the agent just reads whatever
#: ``PROXIALPHA_HOME`` points to.
DEFAULT_HOME = Path("/var/lib/proxialpha")


class AgentSettings(BaseSettings):
    """Typed view of the agent process's environment."""

    model_config = SettingsConfigDict(
        env_prefix="PROXIALPHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Ignore unknown PROXIALPHA_* vars so adding a new field in
        # a future release doesn't crash older agent deployments
        # that still have the old var in their env.
        extra="ignore",
    )

    # -----------------------------------------------------------------
    # Control plane
    # -----------------------------------------------------------------
    control_plane_url: str = Field(
        default="https://app.proxiant.io",
        description=(
            "Base URL of the ProxiAlpha control plane. No trailing "
            "slash — the agent normalises it anyway but mistakes "
            "here are confusing to debug."
        ),
    )

    # -----------------------------------------------------------------
    # Enrollment
    # -----------------------------------------------------------------
    install_token: Optional[str] = Field(
        default=None,
        description=(
            "One-shot install token from the dashboard. Used on "
            "first boot when no license exists on disk. After the "
            "first successful enroll() the license lives in "
            "``$PROXIALPHA_HOME/license`` and this env var can be "
            "unset."
        ),
    )

    # -----------------------------------------------------------------
    # Storage
    # -----------------------------------------------------------------
    home: Path = Field(
        default=DEFAULT_HOME,
        description=(
            "Persistent data directory. Holds the license file, "
            "fingerprint, diary, and Prometheus state files."
        ),
    )

    # -----------------------------------------------------------------
    # JWT verification
    # -----------------------------------------------------------------
    public_key_path: Optional[Path] = Field(
        default=None,
        description=(
            "Override path to the control plane's RS256 public key. "
            "Set to the bundled ``keys/dev_pub.pem`` when a JWKS "
            "URL is not configured. Prod installs typically leave "
            "this None and use ``jwks_url`` instead."
        ),
    )
    jwks_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional JWKS endpoint URL. If set, the license client "
            "fetches and caches the control plane's public keys "
            "here instead of reading the bundled PEM. Unused in "
            "Phase 2 — plumbing is in place so Phase 3 can flip it "
            "on without a code change."
        ),
    )

    # -----------------------------------------------------------------
    # Local HTTP surface
    # -----------------------------------------------------------------
    health_host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the local /health and /metrics "
            "endpoints. MUST be a loopback address — never "
            "0.0.0.0. Enforced by the health server itself."
        ),
    )
    health_port: int = Field(
        default=9877,
        description="Bind port for the /health + /metrics HTTP server.",
    )

    # -----------------------------------------------------------------
    # Logging
    # -----------------------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="Root log level. One of DEBUG, INFO, WARNING, ERROR.",
    )

    # -----------------------------------------------------------------
    # Validators
    # -----------------------------------------------------------------

    @field_validator("control_plane_url")
    @classmethod
    def _normalise_control_plane_url(cls, value: str) -> str:
        """Drop the trailing slash so URL-join logic stays simple.

        The task spec explicitly calls this out: "Do not trust
        settings.control_plane_url to have no trailing slash —
        normalise it". Doing the normalisation inside the setting
        means every caller sees the same shape.
        """
        if not value:
            raise ValueError("control_plane_url must not be empty")
        return value.rstrip("/")

    @field_validator("health_host")
    @classmethod
    def _enforce_loopback(cls, value: str) -> str:
        """Reject non-loopback bind addresses at config-parse time.

        The health server also re-checks this at runtime, but
        catching it here gives a clear error at boot rather than
        deep in the uvicorn stack.
        """
        loopbacks = {"127.0.0.1", "::1", "localhost"}
        if value not in loopbacks:
            raise ValueError(
                f"health_host must be a loopback address (got {value!r}); "
                f"allowed: {sorted(loopbacks)}"
            )
        return value

    # -----------------------------------------------------------------
    # Derived paths
    # -----------------------------------------------------------------

    @property
    def license_path(self) -> Path:
        """Absolute path to the persisted license JWT file."""
        return self.home / "license"

    @property
    def fingerprint_path(self) -> Path:
        """Absolute path to the persisted machine fingerprint file."""
        return self.home / "fingerprint"


@lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Return a process-wide cached :class:`AgentSettings`.

    Tests that want to override env vars should clear the cache with
    ``get_settings.cache_clear()`` between cases.
    """
    return AgentSettings()


__all__ = ["AgentSettings", "DEFAULT_HOME", "get_settings"]
