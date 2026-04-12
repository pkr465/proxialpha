"""Agent-side license JWT management.

This module is the *verify-only* companion to :mod:`core.jwt_keys`.
The control plane **signs** license tokens with a private RS256 key;
agents **verify** them using a bundled public key (or a JWKS URL in
production). Agents must never hold the private key — that's the
whole point of asymmetric signing — so this module is intentionally
limited to the verify side.

Responsibilities
----------------

* Load, verify, and cache a license JWT on disk.
* Enroll a fresh agent against the control plane using a one-shot
  install token.
* Persist licenses atomically with ``0600`` permissions so a
  shared-host deployment can't leak the token between UIDs.
* Maintain a stable machine fingerprint across restarts — the
  supervisor uses this on every heartbeat to prove "same box".

Public surface
--------------

* :class:`License` — dataclass view of a verified token's claims.
* :class:`LicenseError` — verification / I/O failures. Supervisor
  maps this to the REVOKED mode and an exit-1.
* :class:`LicenseClient` — the main class. Tests and the supervisor
  both talk to one of these.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import jwt as _pyjwt
    from jwt import PyJWTError
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PyJWT is required. Install: pip install 'PyJWT>=2.0'"
    ) from exc


log = logging.getLogger(__name__)


#: Name of the expected issuer claim. Must match
#: :data:`core.jwt_keys.TOKEN_ISSUER` on the control plane.
EXPECTED_ISSUER = "proxialpha-control-plane"

#: Hard ceiling on clock skew between the agent and the control
#: plane. The heartbeat endpoint also enforces this on the server
#: side — mirror it here so the agent fails closed before even
#: attempting to call the server with a skewed clock.
CLOCK_SKEW_LEEWAY = timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class LicenseError(Exception):
    """Raised when a license cannot be loaded, verified, or persisted.

    The :attr:`reason` tag disambiguates the failure so the
    supervisor's mode machine can decide whether to exit-1
    (REVOKED) or retry (TRANSIENT). Values are kept short and
    stable so they show up cleanly in structured logs.
    """

    #: Short reason tag — one of ``"expired"``, ``"signature"``,
    #: ``"not_before"``, ``"issuer"``, ``"malformed"``,
    #: ``"fingerprint"``, ``"io"``, ``"network"``, ``"enrollment"``.
    reason: str

    def __init__(self, message: str, *, reason: str = "malformed") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class License:
    """A verified agent license JWT, flattened into a dataclass.

    Instances are immutable — the license client builds a new one
    whenever a fresh token comes in from a heartbeat response. The
    supervisor holds a reference to the current one and reads its
    fields from any thread (dataclass instances are safe for
    concurrent reads).
    """

    raw: str
    org_id: str
    agent_id: str
    fingerprint: str
    entitlements_snapshot: Dict[str, Any] = field(default_factory=dict)
    issued_at: Optional[datetime] = None
    not_before: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    grace_until: Optional[datetime] = None

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        """Return True if the license's ``exp`` claim has passed."""
        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return current >= self.expires_at


# ---------------------------------------------------------------------------
# License client
# ---------------------------------------------------------------------------


class LicenseClient:
    """Load, verify, rotate, and persist agent license JWTs.

    The client is stateful only in that it caches the resolved
    public key — all disk I/O is idempotent so it's safe to share
    one instance across the supervisor and the heartbeat client.

    Parameters
    ----------
    public_key_pem
        Raw PEM-encoded public key bytes. The supervisor reads this
        from either a file path (``settings.public_key_path``) or a
        bundled package resource (``keys/dev_pub.pem``). Tests pass
        the bytes they just generated from a throwaway keypair.
    license_path
        Where to read/write the persisted license file. Typically
        ``$PROXIALPHA_HOME/license``.
    fingerprint_path
        Where to read/write the persisted machine fingerprint.
        Typically ``$PROXIALPHA_HOME/fingerprint``.
    now
        Injectable clock. Tests pass a callable that returns a
        fixed datetime; production leaves this as None and the
        client uses ``datetime.now(timezone.utc)``.
    """

    def __init__(
        self,
        *,
        public_key_pem: bytes,
        license_path: Path,
        fingerprint_path: Path,
        now: Optional[Callable[[], datetime]] = None,
        jwks_url: Optional[str] = None,
        jwks_cache_seconds: int = 600,
        http_client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        if not public_key_pem:
            raise LicenseError(
                "public_key_pem is empty", reason="malformed"
            )
        self._public_key_pem = public_key_pem
        self._license_path = Path(license_path)
        self._fingerprint_path = Path(fingerprint_path)
        self._now = now or (lambda: datetime.now(timezone.utc))
        # JWKS state — kept on the instance so the supervisor can
        # share one client across the heartbeat loop and the
        # in-memory cache survives across heartbeats.
        self._jwks_url = jwks_url
        self._jwks_cache_seconds = jwks_cache_seconds
        self._jwks_http_client_factory = http_client_factory
        self._jwks_cache: Dict[str, bytes] = {}
        self._jwks_cache_expires_at: Optional[datetime] = None

    # -----------------------------------------------------------------
    # Fingerprint
    # -----------------------------------------------------------------

    def fingerprint(self) -> str:
        """Return the stable machine fingerprint, generating one if missing.

        The fingerprint is a UUID4 written once and re-read on every
        subsequent call. We deliberately do NOT derive it from
        hardware serial numbers or MAC addresses — those break on
        container re-creates and leak identifying info. A persisted
        random UUID gives us all the stability we need.
        """
        if self._fingerprint_path.exists():
            text = self._fingerprint_path.read_text(encoding="utf-8").strip()
            if text:
                return text
            # Empty file (corrupted) — fall through to regenerate.
            log.warning(
                "fingerprint file %s exists but is empty; regenerating",
                self._fingerprint_path,
            )
        fp = uuid.uuid4().hex
        self._fingerprint_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._fingerprint_path, fp, mode=0o600)
        log.info("generated new fingerprint at %s", self._fingerprint_path)
        return fp

    # -----------------------------------------------------------------
    # Load / persist
    # -----------------------------------------------------------------

    def load_from_disk(self, path: Optional[Path] = None) -> License:
        """Read the license JWT from disk and verify it.

        Returns a fully-verified :class:`License` or raises
        :class:`LicenseError` with a short reason tag. On
        ``reason == "io"`` (file missing) the supervisor knows to
        fall back to the enrollment path; anything else is fatal.
        """
        target = Path(path) if path is not None else self._license_path
        if not target.exists():
            raise LicenseError(
                f"license file not found at {target}", reason="io"
            )
        try:
            token_str = target.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LicenseError(
                f"failed to read license file {target}: {exc}", reason="io"
            ) from exc
        if not token_str:
            raise LicenseError(
                f"license file {target} is empty", reason="malformed"
            )
        return self.verify(token_str)

    def persist(
        self,
        license_or_token: License | str,
        path: Optional[Path] = None,
    ) -> Path:
        """Atomically write a license JWT to disk with ``0600`` permissions.

        Accepts either a :class:`License` dataclass (writes the
        ``raw`` field) or the raw token string for callers that
        haven't bothered to decode it yet.

        The write is atomic in the POSIX sense — we create a
        temporary file in the same directory, write + fsync +
        chmod, then ``os.replace`` into the final name. A crash at
        any point either leaves the old file untouched or the new
        file fully written; never a partial write.
        """
        token_str = (
            license_or_token.raw
            if isinstance(license_or_token, License)
            else license_or_token
        )
        if not token_str:
            raise LicenseError("persist: empty token", reason="malformed")

        target = Path(path) if path is not None else self._license_path
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, token_str, mode=0o600)
        log.info("persisted license to %s", target)
        return target

    @staticmethod
    def _atomic_write(target: Path, text: str, *, mode: int) -> None:
        """Write ``text`` to ``target`` atomically with the requested perms.

        Uses ``tempfile.NamedTemporaryFile(delete=False)`` in the
        same directory as the target so ``os.replace`` is a true
        rename (not a cross-filesystem copy). Chmod runs on the
        temp file BEFORE the rename so the final file never
        exists on-disk with looser permissions, even for a
        microsecond.
        """
        directory = target.parent
        fd, tmp_path = tempfile.mkstemp(
            prefix=".proxialpha-", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # fsync fails on some exotic filesystems (tmpfs
                    # overlays, certain test runners). Not fatal —
                    # the replace below still preserves atomicity.
                    pass
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup; do not mask the original exception.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -----------------------------------------------------------------
    # JWKS resolution (P0-3)
    # -----------------------------------------------------------------

    def _bundled_key_fingerprint(self) -> str:
        """Return the SHA-256 fingerprint of the bundled public key.

        Matches :func:`core.jwt_keys.key_fingerprint` on the server
        side: 12 hex chars of SHA-256(public_pem). Used to decide
        whether the bundled key matches the JWT header's ``kid``.
        """
        import hashlib

        return hashlib.sha256(self._public_key_pem).hexdigest()[:12]

    def _resolve_public_key_for(self, token_str: str) -> bytes:
        """Pick the right public key for verifying ``token_str``.

        Algorithm:

        1. Read the unverified ``kid`` from the JWT header.
        2. If absent → use the bundled key (legacy tokens).
        3. If it matches the bundled key's fingerprint → use it.
        4. Otherwise consult the JWKS cache. If the cache is fresh
           and contains the kid, return that PEM.
        5. Otherwise refresh the JWKS, retry the lookup, and return
           the matching PEM. On any failure (no jwks_url, network
           error, missing kid) fall through to the bundled key — the
           subsequent ``decode`` call will raise InvalidSignatureError
           which we map to ``LicenseError(reason="signature")``.
        """
        try:
            unverified = _pyjwt.get_unverified_header(token_str)
        except _pyjwt.DecodeError:
            return self._public_key_pem
        kid = unverified.get("kid") if isinstance(unverified, dict) else None
        if not kid:
            return self._public_key_pem
        if kid == self._bundled_key_fingerprint():
            return self._public_key_pem

        cached = self._jwks_lookup(kid)
        if cached is not None:
            return cached

        self._refresh_jwks()
        cached = self._jwks_lookup(kid)
        if cached is not None:
            return cached
        # Last resort — bundled key. The verify will likely fail
        # with reason="signature", which is the correct outcome.
        return self._public_key_pem

    def _jwks_lookup(self, kid: str) -> Optional[bytes]:
        """Look up a kid in the in-process JWKS cache.

        Returns ``None`` if the cache is empty, expired, or missing
        the kid. Caller is responsible for refreshing on miss.
        """
        if self._jwks_cache_expires_at is None:
            return None
        if self._now() >= self._jwks_cache_expires_at:
            return None
        return self._jwks_cache.get(kid)

    def _refresh_jwks(self) -> None:
        """Fetch the JWKS endpoint and rebuild the cache.

        Silent on failure — we never raise from here. The caller
        falls back to the bundled key, which is the correct
        degraded-mode behaviour: if the agent can't reach the JWKS
        endpoint right now, the next verify will retry on the next
        heartbeat.
        """
        if not self._jwks_url:
            return
        try:
            jwks_doc = self._http_get_json(self._jwks_url)
        except Exception as exc:
            log.warning("jwks: failed to fetch %s: %s", self._jwks_url, exc)
            return
        if not isinstance(jwks_doc, dict):
            return
        keys = jwks_doc.get("keys")
        if not isinstance(keys, list):
            return

        new_cache: Dict[str, bytes] = {}
        for entry in keys:
            if not isinstance(entry, dict):
                continue
            kid = entry.get("kid")
            if not kid:
                continue
            pem = self._jwk_to_pem(entry)
            if pem is not None:
                new_cache[kid] = pem
        self._jwks_cache = new_cache
        self._jwks_cache_expires_at = self._now() + timedelta(
            seconds=self._jwks_cache_seconds
        )
        log.info(
            "jwks: refreshed %d key(s) from %s", len(new_cache), self._jwks_url
        )

    def _http_get_json(self, url: str) -> Any:
        """GET ``url`` and return parsed JSON. Stub-friendly for tests."""
        if self._jwks_http_client_factory is not None:
            factory = self._jwks_http_client_factory
        else:
            import httpx

            def factory() -> Any:
                return httpx.Client(timeout=10.0)

        with factory() as client:
            resp = client.get(url)
            status = getattr(resp, "status_code", None)
            if status != 200:
                raise LicenseError(
                    f"jwks fetch returned {status}", reason="network"
                )
            return resp.json()

    @staticmethod
    def _jwk_to_pem(jwk: Dict[str, Any]) -> Optional[bytes]:
        """Convert a single RSA JWK entry to a PEM byte string.

        We only handle ``kty == "RSA"`` because that's all the
        control plane ever issues. Anything else returns None and
        the caller skips it.
        """
        if jwk.get("kty") != "RSA":
            return None
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import base64

            def _b64u(data: str) -> int:
                pad = "=" * (-len(data) % 4)
                raw = base64.urlsafe_b64decode(data + pad)
                return int.from_bytes(raw, "big")

            n = _b64u(jwk["n"])
            e = _b64u(jwk["e"])
            pub = rsa.RSAPublicNumbers(e=e, n=n).public_key()
            return pub.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("jwks: failed to materialise jwk: %s", exc)
            return None

    # -----------------------------------------------------------------
    # Verify
    # -----------------------------------------------------------------

    def verify(self, token_str: str) -> License:
        """Verify a JWT and return a :class:`License` on success.

        Checks performed:

        * RS256 signature against the resolved public key. If the
          token's header carries a ``kid`` that doesn't match the
          bundled key's fingerprint AND a ``jwks_url`` is configured,
          we fetch the JWKS (with a 10-minute in-process cache) and
          retry the verify against the matching public key. This is
          how the agent picks up server-side key rotation without a
          restart.
        * ``iss`` equals ``proxialpha-control-plane``.
        * ``exp``, ``nbf``, ``iat`` are present and sane within
          a 5-minute leeway.
        * ``agent_fingerprint`` matches the local fingerprint.

        Any failure raises :class:`LicenseError` with a stable
        reason tag.
        """
        if not token_str or "." not in token_str:
            raise LicenseError("license is empty or malformed", reason="malformed")

        # Resolve which public key to verify against. The token's
        # header may contain a ``kid``; if it doesn't match our
        # bundled key we ask the JWKS resolver for the right one.
        public_pem = self._resolve_public_key_for(token_str)

        try:
            claims = _pyjwt.decode(
                token_str,
                public_pem,
                algorithms=["RS256"],
                issuer=EXPECTED_ISSUER,
                leeway=int(CLOCK_SKEW_LEEWAY.total_seconds()),
                options={
                    "require": ["exp", "iat", "nbf", "iss"],
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_signature": True,
                },
            )
        except _pyjwt.ExpiredSignatureError as exc:
            raise LicenseError("license has expired", reason="expired") from exc
        except _pyjwt.ImmatureSignatureError as exc:
            raise LicenseError(
                "license nbf claim is in the future", reason="not_before"
            ) from exc
        except _pyjwt.InvalidIssuerError as exc:
            raise LicenseError(
                f"license iss claim is not {EXPECTED_ISSUER!r}", reason="issuer"
            ) from exc
        except _pyjwt.InvalidSignatureError as exc:
            raise LicenseError(
                "license signature did not verify", reason="signature"
            ) from exc
        except _pyjwt.InvalidAlgorithmError as exc:
            raise LicenseError(
                "license uses an unacceptable algorithm", reason="signature"
            ) from exc
        except _pyjwt.DecodeError as exc:
            raise LicenseError(
                f"license could not be decoded: {exc}", reason="malformed"
            ) from exc
        except PyJWTError as exc:
            raise LicenseError(
                f"license verification failed: {exc}", reason="malformed"
            ) from exc

        # ---- Claim shape ----
        for required in ("org_id", "sub", "agent_fingerprint"):
            if required not in claims:
                raise LicenseError(
                    f"license missing required claim {required!r}",
                    reason="malformed",
                )

        # ---- Fingerprint binding ----
        # Compare against the local fingerprint file. A mismatch
        # means the token belongs to a different machine — either
        # the license was copied between hosts, or the fingerprint
        # file was deleted and regenerated with a new UUID. Either
        # way, the supervisor maps this to exit-1.
        local_fp = self.fingerprint()
        if claims["agent_fingerprint"] != local_fp:
            raise LicenseError(
                "license agent_fingerprint does not match local fingerprint",
                reason="fingerprint",
            )

        # ---- Build the dataclass ----
        def _claim_dt(key: str) -> Optional[datetime]:
            raw = claims.get(key)
            if raw is None:
                return None
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)

        return License(
            raw=token_str,
            org_id=str(claims["org_id"]),
            agent_id=str(claims["sub"]),
            fingerprint=str(claims["agent_fingerprint"]),
            entitlements_snapshot=dict(claims.get("entitlements_snapshot") or {}),
            issued_at=_claim_dt("iat"),
            not_before=_claim_dt("nbf"),
            expires_at=_claim_dt("exp"),
            grace_until=_claim_dt("grace_until"),
        )

    # -----------------------------------------------------------------
    # Enrollment (first-boot flow)
    # -----------------------------------------------------------------

    def enroll(
        self,
        install_token: str,
        *,
        control_plane_url: str,
        http_client_factory: Optional[Callable[[], Any]] = None,
    ) -> License:
        """Enroll this agent with the control plane and persist the license.

        ``install_token`` is the one-shot token the user copy-pasted
        from the dashboard. The control plane returns a full
        license JWT, which we verify and persist. Subsequent boots
        use :meth:`load_from_disk` and never hit the network.

        Parameters
        ----------
        install_token
            The one-shot install token.
        control_plane_url
            Normalised base URL (no trailing slash).
        http_client_factory
            Optional factory returning a context-managed httpx-like
            client. Tests pass a stub that records calls; production
            leaves this as None and we build a real
            ``httpx.Client``. The factory returns a **sync** client
            because enrollment is a one-shot boot-time operation —
            there is no event loop to call into yet.
        """
        if not install_token:
            raise LicenseError(
                "enroll: install_token is empty", reason="enrollment"
            )
        if http_client_factory is None:
            import httpx

            def _default_factory() -> Any:
                return httpx.Client(timeout=30.0)

            http_client_factory = _default_factory

        enroll_url = control_plane_url.rstrip("/") + "/agent/enroll"
        payload = {
            "install_token": install_token,
            "fingerprint": self.fingerprint(),
        }

        try:
            with http_client_factory() as client:
                resp = client.post(enroll_url, json=payload)
        except Exception as exc:
            raise LicenseError(
                f"enroll: network error talking to {enroll_url}: {exc}",
                reason="network",
            ) from exc

        status_code = getattr(resp, "status_code", None)
        if status_code != 200:
            body_preview = ""
            try:
                body_preview = getattr(resp, "text", "")[:200]
            except Exception:
                pass
            raise LicenseError(
                f"enroll: control plane returned {status_code}: {body_preview}",
                reason="enrollment",
            )

        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise LicenseError(
                f"enroll: response was not JSON: {exc}", reason="enrollment"
            ) from exc

        token = body.get("license")
        if not token:
            raise LicenseError(
                "enroll: response missing 'license' field", reason="enrollment"
            )

        license_obj = self.verify(token)
        self.persist(license_obj)
        log.info(
            "enroll: successfully enrolled agent_id=%s org_id=%s",
            license_obj.agent_id,
            license_obj.org_id,
        )
        return license_obj


__all__ = [
    "CLOCK_SKEW_LEEWAY",
    "EXPECTED_ISSUER",
    "License",
    "LicenseClient",
    "LicenseError",
]
