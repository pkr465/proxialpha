"""Install-token primitives for Phase 2 agent enrollment.

An install token is a one-shot bearer string an admin generates from
the dashboard and hands to whoever is provisioning a new agent. The
plaintext is shown to the admin **once** and then forgotten — only
the SHA-256 hash is persisted.

The agent presents the plaintext to ``POST /agent/enroll``, which
hashes it and looks it up in ``install_tokens`` keyed on the hash.
A token is valid iff:

* the hash exists,
* ``consumed_at IS NULL`` (single use),
* ``expires_at > now()`` (not stale).

On a successful enroll the row is marked ``consumed_at = now()`` so
the same plaintext cannot be replayed against a second agent. We do
this in the same transaction as the agent insert so a crash between
the two leaves nothing partially-applied.

Security notes
--------------

* The plaintext is generated with ``secrets.token_urlsafe(32)`` which
  yields 256 bits of entropy — well above the 128-bit floor for
  bearer tokens.
* The hash is plain SHA-256 (not bcrypt/argon2) because brute-force
  resistance is irrelevant for high-entropy random strings, and we
  need O(1) lookup at heartbeat time.
* The token's plaintext is never logged. The dashboard ``/install-tokens``
  endpoint returns it exactly once in the response body and we rely on
  the user to copy it.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


#: Default lifetime for an install token. Short window forces the
#: admin to redeem it promptly; the dashboard can override per call.
DEFAULT_INSTALL_TOKEN_TTL = timedelta(minutes=10)


def hash_install_token(plaintext: str) -> str:
    """Return the SHA-256 hex digest of an install-token plaintext.

    Used both at issuance time (to store the hash) and at enroll time
    (to look it up). Centralised here so the algorithm can never drift
    between writer and reader.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_install_token_plaintext() -> str:
    """Generate a fresh install-token plaintext.

    256 bits of entropy URL-safe-base64 encoded. The result is short
    enough to fit on one terminal line and survives copy-paste through
    Slack and email without backslash escaping.
    """
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class IssuedInstallToken:
    """Result of issuing a fresh install token.

    The plaintext is exposed exactly once — the caller is responsible
    for showing it to the admin and never logging it.
    """

    plaintext: str
    token_id: uuid.UUID
    expires_at: datetime


async def issue_install_token(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: Optional[uuid.UUID],
    label: Optional[str] = None,
    ttl: timedelta = DEFAULT_INSTALL_TOKEN_TTL,
    now: Optional[datetime] = None,
) -> IssuedInstallToken:
    """Insert a new install token row and return the plaintext + id.

    The transaction is NOT committed here — the caller decides when
    to commit so the dashboard endpoint can wrap it in its own
    transaction with whatever audit-log work it needs.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    expires_at = stamp + ttl
    plaintext = generate_install_token_plaintext()
    token_hash = hash_install_token(plaintext)
    token_id = uuid.uuid4()

    await session.execute(
        text(
            """
            INSERT INTO install_tokens (
                id, org_id, token_hash, label, created_by,
                created_at, expires_at
            )
            VALUES (
                :id, :org_id, :hash, :label, :created_by,
                :created_at, :expires_at
            )
            """
        ),
        {
            "id": str(token_id),
            "org_id": str(org_id),
            "hash": token_hash,
            "label": label,
            "created_by": str(created_by) if created_by else None,
            "created_at": stamp.isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )
    return IssuedInstallToken(
        plaintext=plaintext, token_id=token_id, expires_at=expires_at
    )


@dataclass(frozen=True)
class ValidatedInstallToken:
    """Result of looking up an install token by plaintext."""

    token_id: uuid.UUID
    org_id: uuid.UUID
    expires_at: datetime


class InvalidInstallToken(Exception):
    """Raised by :func:`validate_and_consume_install_token` on failure.

    ``reason`` is one of ``"unknown"``, ``"expired"``, ``"consumed"``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def validate_and_consume_install_token(
    session: AsyncSession,
    *,
    plaintext: str,
    consumed_by_agent: uuid.UUID,
    now: Optional[datetime] = None,
) -> ValidatedInstallToken:
    """Look up, validate, and atomically consume an install token.

    Raises :class:`InvalidInstallToken` on any failure path. On
    success the row's ``consumed_at`` is set in the same transaction
    so a concurrent enroll for the same plaintext loses the race and
    sees ``consumed`` on its second pass.
    """
    if not plaintext:
        raise InvalidInstallToken("unknown")

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    token_hash = hash_install_token(plaintext)
    result = await session.execute(
        text(
            """
            SELECT id, org_id, expires_at, consumed_at
            FROM install_tokens
            WHERE token_hash = :h
            """
        ),
        {"h": token_hash},
    )
    row = result.fetchone()
    if row is None:
        raise InvalidInstallToken("unknown")

    token_id = uuid.UUID(str(row[0]))
    org_id = uuid.UUID(str(row[1]))
    expires_at = _coerce_dt(row[2])
    consumed_at = _coerce_dt(row[3])

    if consumed_at is not None:
        raise InvalidInstallToken("consumed")
    if expires_at is None or stamp >= expires_at:
        raise InvalidInstallToken("expired")

    # Atomic consume — guard with ``WHERE consumed_at IS NULL`` so a
    # concurrent transaction wins exactly one of the two attempts.
    update = await session.execute(
        text(
            """
            UPDATE install_tokens
            SET consumed_at = :now, consumed_by_agent = :agent
            WHERE id = :id AND consumed_at IS NULL
            """
        ),
        {
            "now": stamp.isoformat(),
            "agent": str(consumed_by_agent),
            "id": str(token_id),
        },
    )
    # rowcount == 0 means we lost the race; treat that as ``consumed``.
    if update.rowcount == 0:
        raise InvalidInstallToken("consumed")

    return ValidatedInstallToken(
        token_id=token_id, org_id=org_id, expires_at=expires_at
    )


async def lookup_install_token(
    session: AsyncSession,
    plaintext: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[ValidatedInstallToken]:
    """Validate an install token WITHOUT consuming it.

    Mirrors :func:`validate_and_consume_install_token` for the read-
    only case (P2-7 doctor bundle uploads). The doctor command may
    run mid-enroll, before the install token has been redeemed —
    consuming the token here would prevent the agent from completing
    its actual enroll afterwards. We therefore validate freshness and
    "not already consumed" but do NOT touch the row.

    Returns ``None`` (rather than raising) on any failure path so the
    caller can decide whether to surface a generic 401 or fall through
    to a different auth mechanism. Failures are not distinguished
    because an upload-time bundle endpoint shouldn't leak whether the
    plaintext was unknown vs. expired vs. consumed — that's an
    enumeration vector against the issuance window.
    """
    if not plaintext:
        return None

    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    token_hash = hash_install_token(plaintext)
    result = await session.execute(
        text(
            """
            SELECT id, org_id, expires_at, consumed_at
            FROM install_tokens
            WHERE token_hash = :h
            """
        ),
        {"h": token_hash},
    )
    row = result.fetchone()
    if row is None:
        return None

    expires_at = _coerce_dt(row[2])
    consumed_at = _coerce_dt(row[3])
    # Allow recently-consumed tokens for a short window so a
    # post-enroll doctor run can still authenticate. The 30-minute
    # grace is wide enough to cover the agent's full first-boot loop
    # but short enough that a leaked plaintext can't be replayed
    # against the bundle endpoint indefinitely.
    if consumed_at is not None:
        if (stamp - consumed_at) > timedelta(minutes=30):
            return None
    if expires_at is None:
        return None
    # Pre-consumption: enforce expiry. Post-consumption: ignore expiry
    # because the consumed_at check above already bounds the grace
    # window and a token that was redeemed before its TTL is fine.
    if consumed_at is None and stamp >= expires_at:
        return None

    return ValidatedInstallToken(
        token_id=uuid.UUID(str(row[0])),
        org_id=uuid.UUID(str(row[1])),
        expires_at=expires_at,
    )


def _coerce_dt(value: object) -> Optional[datetime]:
    """Coerce a Postgres-or-SQLite datetime column to tz-aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


__all__ = [
    "DEFAULT_INSTALL_TOKEN_TTL",
    "InvalidInstallToken",
    "IssuedInstallToken",
    "ValidatedInstallToken",
    "generate_install_token_plaintext",
    "hash_install_token",
    "issue_install_token",
    "lookup_install_token",
    "validate_and_consume_install_token",
]
