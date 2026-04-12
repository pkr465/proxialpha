"""Tests for :mod:`proxialpha_agent.license` (Task 07).

Covers the five license-client responsibilities required by the
acceptance criteria:

1. Loading and verifying a valid RS256 token from disk.
2. Rejecting expired tokens with ``reason="expired"``.
3. Rejecting tokens signed by a different keypair with
   ``reason="signature"``.
4. Persisting atomically with ``0600`` permissions.
5. Keeping the machine fingerprint stable across repeated calls.

JWT strategy
------------

We use the real :mod:`core.jwt_keys` module in its dev-fallback
mode (the same path the Task 06 heartbeat tests use). Each test
generates a fresh keypair via ``jwt_keys.reset_cache_for_tests()``
and signs tokens through :func:`core.jwt_keys.sign` — that
exercises the whole RS256 code path end to end without ever
touching a real signing key on disk.
"""
from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Make the repo importable when pytest is invoked from outside.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Force dev-generated keypair path.
os.environ.pop("AGENT_SIGNING_KEY_PATH", None)
os.environ.pop("AGENT_SIGNING_KEY_PEM", None)
os.environ["ENV"] = "dev"

from core import jwt_keys  # noqa: E402
from proxialpha_agent.license import (  # noqa: E402
    License,
    LicenseClient,
    LicenseError,
)


FIXED_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_jwt_keys_cache() -> None:
    """Force jwt_keys to rebuild its keypair for every test.

    Each test gets its own in-memory keypair so signature
    mismatch tests don't pollute subsequent tests.
    """
    jwt_keys.reset_cache_for_tests()
    yield
    jwt_keys.reset_cache_for_tests()


@pytest.fixture
def tmp_agent_home(tmp_path: Path) -> Path:
    """Give each test an isolated PROXIALPHA_HOME-style directory."""
    home = tmp_path / "proxialpha-home"
    home.mkdir()
    return home


@pytest.fixture
def client_factory(tmp_agent_home: Path):
    """Return a factory that builds a fresh LicenseClient pointing at tmp_agent_home.

    Uses a fixed-clock ``now`` callable so expiration tests are
    deterministic — we don't rely on wall-clock time.
    """

    def _make(
        *,
        public_key_pem: bytes = None,
        now: datetime = FIXED_NOW,
    ) -> LicenseClient:
        pem = public_key_pem if public_key_pem is not None else jwt_keys.public_key_pem()
        return LicenseClient(
            public_key_pem=pem,
            license_path=tmp_agent_home / "license",
            fingerprint_path=tmp_agent_home / "fingerprint",
            now=lambda: now,
        )

    return _make


def _sign_license_for(
    *,
    client: LicenseClient,
    org_id: str = "org_acme",
    agent_id: str = "agent_alpha",
    expires_in: timedelta = timedelta(hours=24),
    now: datetime = FIXED_NOW,
    override_fingerprint: str | None = None,
) -> str:
    """Sign a valid license JWT for the given client's fingerprint."""
    fingerprint = override_fingerprint or client.fingerprint()
    claims = {
        "sub": agent_id,
        "org_id": org_id,
        "agent_fingerprint": fingerprint,
        "entitlements_snapshot": {"live_trading": True},
    }
    return jwt_keys.sign(claims, expires_in=expires_in, now=now)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_license_load_valid_token(client_factory, tmp_agent_home: Path) -> None:
    """A valid RS256 token round-trips through persist -> load."""
    client = client_factory()
    token = _sign_license_for(client=client)
    client.persist(token)

    loaded = client.load_from_disk()
    assert isinstance(loaded, License)
    assert loaded.org_id == "org_acme"
    assert loaded.agent_id == "agent_alpha"
    assert loaded.fingerprint == client.fingerprint()
    assert loaded.entitlements_snapshot == {"live_trading": True}
    assert loaded.expires_at is not None
    assert loaded.expires_at > FIXED_NOW


def test_license_load_expired_raises(client_factory) -> None:
    """An expired token surfaces as ``LicenseError(reason="expired")``."""
    client = client_factory()
    # Sign a token whose exp is 1 hour ago relative to the fixed clock.
    # The jwt_keys default leeway in verify is 300s; we use -1h so
    # it's comfortably outside that window.
    old_now = FIXED_NOW - timedelta(hours=25)
    token = _sign_license_for(
        client=client,
        expires_in=timedelta(hours=1),
        now=old_now,
    )
    client.persist(token)

    with pytest.raises(LicenseError) as exc_info:
        client.load_from_disk()
    assert exc_info.value.reason == "expired"


def test_license_load_signature_mismatch_raises(
    client_factory, tmp_agent_home: Path
) -> None:
    """A token signed with a different keypair fails with reason='signature'."""
    # Client #1: real key, signs the token.
    signer_client = client_factory()
    token = _sign_license_for(client=signer_client)
    signer_client.persist(token)

    # Build a different public key and construct a client with it.
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pub = other_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    bad_client = LicenseClient(
        public_key_pem=other_pub,
        license_path=tmp_agent_home / "license",
        fingerprint_path=tmp_agent_home / "fingerprint",
        now=lambda: FIXED_NOW,
    )

    with pytest.raises(LicenseError) as exc_info:
        bad_client.load_from_disk()
    # Signature failure, malformed, or algorithm — all of which map
    # to a hard-fail reason the supervisor treats as non-recoverable.
    assert exc_info.value.reason in ("signature", "malformed")


def test_license_persist_atomic_and_0600(
    client_factory, tmp_agent_home: Path
) -> None:
    """Persisted license file is readable only by owner (0600)."""
    client = client_factory()
    token = _sign_license_for(client=client)
    target = client.persist(token)

    assert target.exists()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0600 perms, got {oct(mode)}"

    # Content matches what we wrote.
    assert target.read_text(encoding="utf-8").strip() == token

    # Re-persisting overwrites the existing file atomically.
    new_token = _sign_license_for(
        client=client, agent_id="agent_beta"
    )
    client.persist(new_token)
    assert target.read_text(encoding="utf-8").strip() == new_token
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_license_fingerprint_stable_across_calls(
    client_factory, tmp_agent_home: Path
) -> None:
    """Fingerprint is persisted to disk and reused across client instances."""
    client1 = client_factory()
    fp1 = client1.fingerprint()
    assert fp1
    assert (tmp_agent_home / "fingerprint").exists()

    # Second call on the same client returns the same value.
    assert client1.fingerprint() == fp1

    # A brand-new client pointed at the same directory also reuses it.
    client2 = client_factory()
    assert client2.fingerprint() == fp1

    # The persisted file is 0600.
    mode = stat.S_IMODE((tmp_agent_home / "fingerprint").stat().st_mode)
    assert mode == 0o600
