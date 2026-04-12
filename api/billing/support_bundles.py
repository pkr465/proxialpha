"""Doctor bundle upload endpoint — P2-7 in the gap analysis.

The customer-facing agent ships a ``proxialpha doctor`` command that
builds a redacted ``.tar.gz`` support bundle (see
:mod:`proxialpha_agent.doctor`). Until P2-7 there was nowhere to send
the bundle — the customer had to email it, which slows the support
loop and silently drops files >10 MB at several mail providers.

This module exposes a single endpoint:

``POST /api/support/bundles``
    Multipart upload of one ``.tar.gz`` file. Returns a row id and a
    deterministic storage URI. Auth is by EITHER an authed Clerk user
    OR an unauthenticated install-token holder (the agent can call
    this endpoint mid-enroll, before its org's first Clerk user
    exists). The auth is decided per-request inside the handler.

Storage
-------

We do **not** put bundle bytes in Postgres. The row in
``support_bundles`` carries a ``storage_uri`` that points at object
storage. The driver behind that URI is selected by the
``SUPPORT_BUNDLE_STORAGE`` setting:

* ``filesystem`` (default in dev) — writes to
  ``$SUPPORT_BUNDLE_DIR`` (default ``/var/lib/proxialpha/bundles``)
  and the URI looks like ``file:///var/lib/proxialpha/bundles/{id}.tar.gz``.
* ``s3`` — writes to ``s3://<bucket>/{id}.tar.gz`` using boto3 if
  available. Falls back to filesystem with a warning if boto3 is
  not installed (so a missing optional dep can't break customer
  uploads).
* ``gcs`` / ``azure`` — placeholder for future provider support;
  raises ``NotImplementedError`` at startup pointing the operator
  back to the runbook.

The driver selection happens at module import time, NOT per-request,
because the boto3 client construction is expensive and the credential
resolution is identical across uploads. Tests override
:data:`_storage_driver` directly.

Bundle validation
-----------------

We refuse to accept bundles that:

* exceed :data:`MAX_BUNDLE_SIZE_BYTES` (mirrors
  ``proxialpha_agent.doctor.MAX_BUNDLE_SIZE_BYTES`` so the agent
  side and the API agree on the cap).
* are not gzip-magic-bytes-prefixed (a quick sanity check; we don't
  fully validate the tar structure here — that happens during the
  support tooling's analysis pass).
* hit the rate limit (5/min/org) — see
  :func:`api.middleware.rate_limit.enforce_bundle_upload_limit`.

We compute the SHA-256 of the bundle bytes during the upload stream
and store the digest hex in ``support_bundles.sha256_hex``. Used by
the support tooling to detect bit-rot and to skip already-analyzed
duplicate uploads.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Tuple

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard ceiling on bundle size. Matches
#: ``proxialpha_agent.doctor.MAX_BUNDLE_SIZE_BYTES`` so the two sides
#: can't drift. If the agent's cap goes up, this one MUST go up too.
MAX_BUNDLE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

#: Gzip magic bytes (RFC 1952 §2.3.1). Used as a cheap front-door
#: validity check; the support tooling does the full tar validation.
_GZIP_MAGIC = b"\x1f\x8b"


# ---------------------------------------------------------------------------
# Storage drivers
# ---------------------------------------------------------------------------


class _StorageDriver:
    """Tiny driver interface used by the upload handler.

    Two methods. Implementations must be safe to call concurrently —
    the route handler does not serialise uploads.
    """

    name: str = "abstract"

    async def write(self, bundle_id: uuid.UUID, data: bytes) -> str:
        """Persist ``data`` and return its storage URI."""
        raise NotImplementedError


class _FilesystemDriver(_StorageDriver):
    """Default driver. Writes bundles to a local directory.

    Suitable for single-host deployments and dev. Production should
    use the S3 driver via ``SUPPORT_BUNDLE_STORAGE=s3``.
    """

    name = "filesystem"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    async def write(self, bundle_id: uuid.UUID, data: bytes) -> str:
        path = self._root / f"{bundle_id}.tar.gz"
        # Sync write inside an async context is fine for 5 MB blobs;
        # the alternative (aiofiles) adds a dep we don't otherwise
        # need. Bundle uploads are rare (single-digit per support
        # ticket), so this isn't a hot path.
        path.write_bytes(data)
        return f"file://{path.absolute()}"


class _S3Driver(_StorageDriver):
    """S3 driver — used when SUPPORT_BUNDLE_STORAGE=s3.

    Falls back to a filesystem driver at construction time if boto3
    isn't installed, so a missing optional dep can't take customer
    uploads down. The fallback logs a WARNING with explicit "set
    SUPPORT_BUNDLE_STORAGE=filesystem to silence this".
    """

    name = "s3"

    def __init__(self, bucket: str) -> None:
        self._bucket = bucket
        try:
            import boto3  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - optional dep
            log.warning(
                "support_bundles: boto3 not installed (%s); falling back to "
                "filesystem driver. Set SUPPORT_BUNDLE_STORAGE=filesystem to "
                "silence this warning.",
                exc,
            )
            self._client = None
        else:
            self._client = boto3.client("s3")

    async def write(self, bundle_id: uuid.UUID, data: bytes) -> str:
        if self._client is None:
            # Fallback path — write to /tmp so the bundle is at least
            # not lost in the boto3-missing scenario.
            tmp = Path("/tmp") / f"{bundle_id}.tar.gz"
            tmp.write_bytes(data)
            return f"file://{tmp.absolute()}"
        key = f"{bundle_id}.tar.gz"
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/gzip",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self._bucket}/{key}"


def _build_default_driver() -> _StorageDriver:
    """Construct the driver indicated by env at module-import time."""
    backend = os.environ.get("SUPPORT_BUNDLE_STORAGE", "filesystem").lower()
    if backend == "s3":
        bucket = os.environ.get("SUPPORT_BUNDLE_S3_BUCKET")
        if not bucket:
            log.warning(
                "support_bundles: SUPPORT_BUNDLE_STORAGE=s3 but "
                "SUPPORT_BUNDLE_S3_BUCKET is empty; using filesystem driver"
            )
            return _FilesystemDriver(_default_fs_root())
        return _S3Driver(bucket)
    if backend in ("gcs", "azure"):
        raise NotImplementedError(
            f"support_bundles: storage backend {backend!r} is not implemented "
            "yet — see docs/runbooks/support-bundles.md for the migration plan"
        )
    return _FilesystemDriver(_default_fs_root())


def _default_fs_root() -> Path:
    return Path(os.environ.get("SUPPORT_BUNDLE_DIR", "/var/lib/proxialpha/bundles"))


#: Module-level driver. Tests override by assigning to
#: ``api.billing.support_bundles._storage_driver``. Wrapped in a
#: try/except so a misconfigured env doesn't fail the import (the
#: route handler will surface the real error per-request).
try:
    _storage_driver: _StorageDriver = _build_default_driver()
except Exception as _drv_exc:  # pragma: no cover - misconfig fallback
    log.error("support_bundles: storage driver init failed: %s", _drv_exc)
    _storage_driver = _FilesystemDriver(Path("/tmp/proxialpha-bundles"))


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class SupportBundleUploadResponse(BaseModel):
    bundle_id: str = Field(..., description="UUID of the support_bundles row.")
    storage_uri: str = Field(
        ...,
        description=(
            "Backend-specific URI where the bundle was persisted. "
            "Opaque to the caller — used internally by the support tooling."
        ),
    )
    size_bytes: int = Field(..., description="Size of the uploaded bundle.")
    sha256_hex: str = Field(..., description="SHA-256 of the bundle bytes.")


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


async def _resolve_org_for_upload(
    request: Request,
    install_token: Optional[str],
    session: AsyncSession,
) -> Tuple[uuid.UUID, Optional[str], Optional[uuid.UUID]]:
    """Return ``(org_id, uploaded_by, agent_id)`` for the upload.

    Two paths:

    1. **Clerk-authed user.** ``request.state.org_id`` is set by the
       Clerk middleware. ``uploaded_by`` is the user's email; no
       agent_id (the dashboard initiated the upload).

    2. **Install-token agent.** ``install_token`` is provided as a
       form field. We validate it against the install_tokens table
       (via :mod:`api.agent.install_tokens`) — but UNLIKE enrollment
       we do NOT consume the token, because the customer may need
       to re-upload before completing enrollment.

    If neither path resolves we raise 401.
    """
    # Path 1 — already-authed Clerk user.
    org_id = getattr(request.state, "org_id", None)
    user = getattr(request.state, "user", None)
    if org_id is not None:
        email = getattr(user, "email", None) if user is not None else None
        return org_id, email, None

    # Path 2 — install-token bearer.
    if install_token:
        try:
            from api.agent.install_tokens import lookup_install_token
        except Exception as exc:
            log.warning("support_bundles: install_tokens module unavailable: %s", exc)
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            validated = await lookup_install_token(session, install_token)
        except Exception as exc:  # pragma: no cover - lookup failure
            log.warning("support_bundles: install_token lookup failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid install token")
        if validated is None:
            raise HTTPException(status_code=401, detail="Invalid install token")
        return validated.org_id, "agent:install_token", None

    raise HTTPException(status_code=401, detail="Authentication required")


# ---------------------------------------------------------------------------
# DB session
# ---------------------------------------------------------------------------


async def _get_billing_session() -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession for support-bundle inserts.

    Reuses the same session-factory plumbing as the other billing
    routers so the BG worker role / RLS context is identical.
    """
    from api.billing.endpoints import _get_billing_session as _delegate

    async for session in _delegate():
        yield session


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


_INSERT_BUNDLE_SQL = text(
    """
    INSERT INTO support_bundles (
        id, org_id, agent_id, uploaded_by, storage_uri,
        size_bytes, sha256_hex, status, uploaded_at
    )
    VALUES (
        :id, :org_id, :agent_id, :uploaded_by, :storage_uri,
        :size_bytes, :sha256_hex, 'received', :uploaded_at
    )
    """
)


@router.post(
    "/support/bundles",
    response_model=SupportBundleUploadResponse,
    status_code=201,
)
async def upload_support_bundle(
    request: Request,
    bundle: UploadFile = File(..., description="The .tar.gz bundle from `proxialpha doctor`."),
    install_token: Optional[str] = Form(
        default=None,
        description="Install-token plaintext, if uploading from an unenrolled agent.",
    ),
    notes: Optional[str] = Form(
        default=None,
        max_length=2000,
        description="Optional free-form note from the operator.",
    ),
    session: AsyncSession = Depends(_get_billing_session),
) -> SupportBundleUploadResponse:
    """Accept and persist a doctor bundle.

    See module docstring for the full contract. The handler:

    1. Resolves the org via Clerk auth or install-token fallback.
    2. Streams the upload, capping at MAX_BUNDLE_SIZE_BYTES, hashing
       as it goes.
    3. Validates the gzip magic bytes.
    4. Persists the bytes via the configured storage driver.
    5. Inserts a ``support_bundles`` row with the metadata.
    6. Returns the row id + storage URI.

    On any failure between steps 4 and 5 the storage object is left
    behind for the cleanup job to GC. We do NOT roll back the storage
    write — losing the bundle is worse than orphaning it.
    """
    # P2-7 + P1-8: rate limit by IP + org. Reuses the token-bucket
    # primitives. Skipped if the rate limiter module isn't installed
    # (trading-only checkout).
    try:
        from api.middleware.rate_limit import enforce_bundle_upload_limit
    except Exception as exc:  # pragma: no cover - optional dep
        log.warning("support_bundles: rate limiter unavailable: %s", exc)
    else:
        enforce_bundle_upload_limit(request)

    org_id, uploaded_by, agent_id = await _resolve_org_for_upload(
        request, install_token, session
    )

    # Stream the upload while hashing and bounding the size.
    # We buffer in memory because the cap is 5 MB and the alternative
    # (spooling to /tmp) creates yet another cleanup obligation.
    sha = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await bundle.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BUNDLE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "bundle_too_large",
                    "max_bytes": MAX_BUNDLE_SIZE_BYTES,
                },
            )
        sha.update(chunk)
        chunks.append(chunk)
    data = b"".join(chunks)

    if not data.startswith(_GZIP_MAGIC):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_bundle",
                "reason": "missing gzip magic bytes",
            },
        )

    bundle_id = uuid.uuid4()
    digest = sha.hexdigest()

    # Persist to object storage FIRST. If the storage write fails we
    # return 500 without inserting a row, so the table never has
    # ghost entries pointing at non-existent objects.
    try:
        storage_uri = await _storage_driver.write(bundle_id, data)
    except Exception as exc:
        log.error(
            "support_bundles: storage driver %s write failed for %s: %s",
            _storage_driver.name,
            bundle_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="bundle storage failed")

    # Insert the metadata row. The `notes` column is updated in a
    # follow-up only if non-empty so the dashboard's NULL-vs-empty
    # rendering stays clean.
    await session.execute(
        _INSERT_BUNDLE_SQL,
        {
            "id": str(bundle_id),
            "org_id": str(org_id),
            "agent_id": str(agent_id) if agent_id else None,
            "uploaded_by": uploaded_by,
            "storage_uri": storage_uri,
            "size_bytes": total,
            "sha256_hex": digest,
            "uploaded_at": datetime.now(timezone.utc),
        },
    )
    if notes:
        await session.execute(
            text("UPDATE support_bundles SET notes = :n WHERE id = :id"),
            {"n": notes, "id": str(bundle_id)},
        )
    await session.commit()

    log.info(
        "support_bundles: accepted bundle %s org=%s size=%s sha256=%s",
        bundle_id,
        org_id,
        total,
        digest,
    )

    return SupportBundleUploadResponse(
        bundle_id=str(bundle_id),
        storage_uri=storage_uri,
        size_bytes=total,
        sha256_hex=digest,
    )
