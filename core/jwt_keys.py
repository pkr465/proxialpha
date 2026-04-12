"""RS256 signing/verification for agent license tokens.

This module is the **only** place in the control plane that loads a
signing key or performs JWT operations for the agent protocol. Every
other module should import :func:`sign`, :func:`verify`, or
:exc:`InvalidToken` from here.

Contract
--------

The public surface is small by design:

``sign(claims, *, expires_in) -> str``
    Encode ``claims`` with RS256 using the process signing key and
    return a compact JWT. Sets ``iat``, ``nbf``, ``exp``, and ``iss``
    if the caller hasn't already.

``verify(token, *, leeway=300) -> dict``
    Decode and verify the token's signature, issuer, and time
    claims. Raises :exc:`InvalidToken` on any failure with a short
    ``reason`` attribute that the heartbeat handler puts into its
    error response (but nothing more detailed — see the Phase 2
    PRD on token error surface).

``public_key_pem() -> bytes``
    Return the PEM-encoded public key so the agent side can embed
    it on first install. This is the only "export" function.

Key loading
-----------

Signing key resolution, in priority order:

1. ``AGENT_SIGNING_KEY_PATH`` env var → PEM file on disk. This is
   the production path. The file MUST be PKCS#8 RSA private key;
   encrypted keys are not supported (KMS integration lives in a
   future task).
2. ``AGENT_SIGNING_KEY_PEM`` env var → the raw PEM content inline.
   Useful for Kubernetes Secrets that mount as env vars.
3. Dev fallback: generate a fresh 2048-bit keypair at process
   startup and keep it in memory. Only activates when ``ENV=dev``
   (or unset); in ``ENV=prod`` the server REFUSES TO START if no
   real key is found. This is a deliberate guardrail — silently
   generating keys in prod would make every deploy invalidate
   every agent's current token.

The loaded keypair is cached at module level. To rotate in dev,
restart the process. Production rotation is out of scope for Phase
1; it'll land in Task 07 alongside a JWKS endpoint.

Why RS256 and not HS256
-----------------------

Agents ship with the **public** key embedded in their binary so
they can verify signatures without ever holding the secret. This
means a compromised agent binary cannot forge new tokens. HS256
would require every agent to hold the symmetric key, which is a
supply-chain disaster waiting to happen.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

# We use PyJWT (already installed). python-jose would also work but
# PyJWT is the more commonly-audited library for this use case.
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class InvalidToken(Exception):
    """Raised by :func:`verify` on any failure.

    The ``reason`` attribute is a short machine-readable tag that the
    heartbeat handler puts directly into its JSON error response —
    typical values are ``"expired"``, ``"signature"``, ``"not_before"``,
    ``"algorithm"``, ``"issuer"``, ``"malformed"``. Callers SHOULD NOT
    include the original exception message in user-facing output.
    """

    def __init__(self, reason: str, *, underlying: Optional[Exception] = None):
        super().__init__(reason)
        self.reason = reason
        self.underlying = underlying


class SigningKeyUnavailable(RuntimeError):
    """Raised when the process cannot resolve a signing key at startup.

    In production (``ENV=prod``) this aborts server boot. In dev it
    never fires — the dev fallback generates a keypair in memory.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The JWT ``iss`` claim stamped on every token we sign and required
#: on every token we verify. Keep in sync with the agent-side verifier.
TOKEN_ISSUER = "proxialpha-control-plane"

#: Algorithm used for all agent license tokens. We REFUSE to decode
#: tokens signed with any other algorithm — an attacker who finds
#: the public key could otherwise forge an HS256 token using the
#: public key as the shared secret (classic PyJWT footgun).
ALGORITHM = "RS256"

#: Clock-skew tolerance applied during verify. Five minutes is the
#: industry-standard default; see the acceptance criteria in the
#: Task 06 prompt.
DEFAULT_LEEWAY_SECONDS = 300


# ---------------------------------------------------------------------------
# Key pair state
# ---------------------------------------------------------------------------


@dataclass
class _KeyMaterial:
    """Cached PEM bytes and a nice identifier for logs."""

    private_pem: bytes
    public_pem: bytes
    fingerprint: str
    source: str  # "env_path", "env_pem", "dev_generated"


_cached: Optional[_KeyMaterial] = None
_cached_previous: Optional[_KeyMaterial] = None
_previous_loaded: bool = False  # explicit "we tried" flag (None is valid)
_provider_singleton = None  # constructed lazily by _provider()


def _compute_fingerprint(public_pem: bytes) -> str:
    """Return a short human-readable ID for a public key.

    Used for log lines at startup so you can verify which key the
    process booted with (e.g. after a rotation). We truncate the
    SHA-256 to 12 hex chars — collision risk is nil for the tiny
    population of keys that exists.
    """
    import hashlib

    return hashlib.sha256(public_pem).hexdigest()[:12]


def _provider():
    """Return the configured :class:`SigningKeyProvider` instance.

    Lazily resolved on first use so the import-time cost of the
    settings/provider modules is paid once. Tests can monkeypatch
    ``_provider_singleton`` between scenarios via
    :func:`reset_cache_for_tests`.
    """
    global _provider_singleton
    if _provider_singleton is None:
        from core.key_providers import get_provider

        try:
            from core.settings import get_settings

            name = getattr(get_settings(), "signing_key_provider", "file")
        except Exception:  # pragma: no cover - settings unavailable
            name = "file"
        _provider_singleton = get_provider(name)
    return _provider_singleton


def _load_from_provider(which: str) -> Optional[_KeyMaterial]:
    """Resolve a key from the active provider and materialise it.

    ``which`` is either ``"active"`` or ``"previous"``. The provider
    returns raw bytes + a tag; we run the cryptography parse here so
    every provider gets the same uniform fingerprint logging.
    """
    prov = _provider()
    raw = prov.load_active() if which == "active" else prov.load_previous()
    if raw is None:
        return None
    return _materialize(raw.private_pem, source=raw.source)


def _load_from_env_path() -> Optional[_KeyMaterial]:
    """Legacy shim — kept so existing tests that patch this still work.

    New code should call :func:`_load_from_provider` instead. This
    function delegates through the provider so the env-var path is
    still honoured even when this entry point is used directly.
    """
    return _load_from_provider("active")


def _load_from_env_pem() -> Optional[_KeyMaterial]:
    """Legacy shim — see :func:`_load_from_env_path`."""
    return None  # provider handles both paths in one shot


def _load_previous_key() -> Optional[_KeyMaterial]:
    """Load the **previous** signing key for JWKS overlap during rotation.

    Resolution mirrors the active key, but with the ``PREVIOUS_``
    prefix so an operator can run a key rotation by:

    1. Setting ``AGENT_SIGNING_KEY_PATH=/etc/proxialpha/new.pem`` and
       ``AGENT_PREVIOUS_SIGNING_KEY_PATH=/etc/proxialpha/old.pem``.
    2. Restarting the API.
    3. Waiting one JWKS TTL (10 minutes by default) so every agent
       has refreshed its cache and now accepts tokens signed with
       either key.
    4. Removing ``AGENT_PREVIOUS_SIGNING_KEY_PATH`` and restarting
       the API to drop the old key from the JWKS set.

    Returning ``None`` (the typical state) means JWKS publishes only
    the active key.
    """
    # Delegate to the provider abstraction so KMS / Vault / file all
    # share the same code path. The env-var fallbacks below are kept
    # for backwards compatibility with existing test fixtures that
    # patch the env directly without going through settings.
    via_provider = _load_from_provider("previous")
    if via_provider is not None:
        return via_provider
    path = os.environ.get("AGENT_PREVIOUS_SIGNING_KEY_PATH")
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return _materialize(fh.read(), source="env_path_previous")
    raw = os.environ.get("AGENT_PREVIOUS_SIGNING_KEY_PEM")
    if raw:
        priv_pem = raw.encode("utf-8") if isinstance(raw, str) else raw
        return _materialize(priv_pem, source="env_pem_previous")
    return None


def _generate_dev_keypair() -> _KeyMaterial:
    """Create a fresh in-memory 2048-bit RSA keypair for dev use.

    Only called when ``ENV != prod`` and neither real-key env var is
    set. The keypair lives only for the lifetime of the process;
    restarting the server invalidates every agent's current token,
    which is fine in dev.
    """
    log.warning(
        "jwt_keys: generating EPHEMERAL RSA keypair — this process is "
        "the only verifier and tokens become invalid on restart. "
        "Set AGENT_SIGNING_KEY_PATH for a stable key."
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _materialize(priv_pem, source="dev_generated")


def _materialize(priv_pem: bytes, *, source: str) -> _KeyMaterial:
    """Parse a private key PEM and derive the public PEM from it.

    This runs exactly once per process in the normal path, so the
    parsing cost is irrelevant. We hold the bytes (not the parsed
    key object) because PyJWT's ``encode`` accepts PEM directly and
    that's the simplest interop.
    """
    try:
        priv_key = serialization.load_pem_private_key(priv_pem, password=None)
    except Exception as exc:
        raise SigningKeyUnavailable(
            f"failed to parse agent signing key from {source}: {exc}"
        ) from exc
    if not isinstance(priv_key, rsa.RSAPrivateKey):
        raise SigningKeyUnavailable(
            f"agent signing key from {source} is not RSA "
            f"(got {type(priv_key).__name__}) — RS256 requires an RSA key"
        )
    pub_pem = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fp = _compute_fingerprint(pub_pem)
    log.info("jwt_keys: loaded signing key source=%s fingerprint=%s", source, fp)
    return _KeyMaterial(
        private_pem=priv_pem,
        public_pem=pub_pem,
        fingerprint=fp,
        source=source,
    )


def _load() -> _KeyMaterial:
    """Resolve the signing key at first use, then cache at module scope."""
    global _cached
    if _cached is not None:
        return _cached

    mat = _load_from_env_path() or _load_from_env_pem()
    if mat is None:
        env = os.environ.get("ENV", "dev").lower()
        if env == "prod":
            raise SigningKeyUnavailable(
                "agent signing key is required in prod: set "
                "AGENT_SIGNING_KEY_PATH or AGENT_SIGNING_KEY_PEM"
            )
        mat = _generate_dev_keypair()

    _cached = mat
    return mat


def _load_previous() -> Optional[_KeyMaterial]:
    """Load the previous-key material once and cache it.

    Returns ``None`` if no previous key is configured. The cache uses
    a separate ``_previous_loaded`` flag because ``None`` is itself a
    valid cached value (no previous key) and we don't want to retry
    the env-var resolution on every JWKS request.
    """
    global _cached_previous, _previous_loaded
    if _previous_loaded:
        return _cached_previous
    try:
        _cached_previous = _load_previous_key()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("jwt_keys: failed to load previous key: %s", exc)
        _cached_previous = None
    _previous_loaded = True
    return _cached_previous


def reset_cache_for_tests() -> None:
    """Drop the cached keypair so the next call to :func:`_load` reloads.

    Tests call this between scenarios that swap the env vars. Not part
    of the public API but intentionally not prefixed with ``_`` so
    pytest code reads cleanly.
    """
    global _cached, _cached_previous, _previous_loaded, _provider_singleton
    _cached = None
    _cached_previous = None
    _previous_loaded = False
    _provider_singleton = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def public_key_pem() -> bytes:
    """Return the PEM-encoded public key (SubjectPublicKeyInfo)."""
    return _load().public_pem


def key_fingerprint() -> str:
    """Return the 12-char SHA-256 fingerprint of the active public key."""
    return _load().fingerprint


def sign(
    claims: Dict[str, Any],
    *,
    expires_in: timedelta,
    now: Optional[datetime] = None,
) -> str:
    """Encode ``claims`` as an RS256 JWT.

    Parameters
    ----------
    claims
        Arbitrary claims dict. Callers set ``sub``, ``org_id``, and
        any token-specific payload (like ``entitlements_snapshot``).
        This function stamps ``iat``, ``nbf``, ``exp``, and ``iss``.
    expires_in
        How long until the token expires. Agent license tokens use
        24 hours; tests pass shorter windows.
    now
        Override for the "current time" stamped on the token. Tests
        pass a fixed datetime; production omits this.

    The caller is responsible for NOT passing PII in ``claims`` —
    this function does not filter. See the Task 06 prompt's "Do not"
    list: Stripe customer IDs, emails, etc. are forbidden in JWTs.
    """
    mat = _load()
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)

    payload: Dict[str, Any] = dict(claims)
    payload.setdefault("iss", TOKEN_ISSUER)
    payload.setdefault("iat", int(stamp.timestamp()))
    payload.setdefault("nbf", int(stamp.timestamp()))
    payload.setdefault("exp", int((stamp + expires_in).timestamp()))
    # Auto-stamp jti so heartbeat replay detection has something to
    # key on. Callers MAY override by passing ``jti`` in ``claims``;
    # production callers should not.
    if "jti" not in payload:
        import uuid as _uuid

        payload["jti"] = _uuid.uuid4().hex

    # Stamp the JWT header's ``kid`` with the active key's fingerprint
    # so the agent's JWKS resolver can pick the right public key out
    # of a multi-key set during rotation. Without this, an agent that
    # has cached a stale JWKS doesn't know which entry to trust.
    headers = {"kid": mat.fingerprint}
    token = jwt.encode(
        payload, mat.private_pem, algorithm=ALGORITHM, headers=headers
    )
    # PyJWT 2.x returns str; PyJWT 1.x returns bytes. Normalise.
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


def verify(
    token: str,
    *,
    leeway: int = DEFAULT_LEEWAY_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Verify a token's signature, issuer, and time claims.

    Parameters
    ----------
    token
        The compact JWT string. If it's empty or not a string, we
        raise :exc:`InvalidToken("malformed")` without touching PyJWT.
    leeway
        Clock-skew tolerance in seconds. Default 300 (5 minutes)
        per ADR-003.
    now
        Override for the "current time" used during verification.
        Tests pass a fixed datetime; production omits this. PyJWT
        doesn't accept a ``now`` parameter on its public API, so we
        temporarily patch the leeway and ``iat`` check by doing the
        time checks ourselves after decode.

    Returns
    -------
    dict
        The decoded claims on success.

    Raises
    ------
    InvalidToken
        With ``.reason`` set to one of ``"malformed"``, ``"expired"``,
        ``"not_before"``, ``"signature"``, ``"algorithm"``,
        ``"issuer"``.
    """
    if not isinstance(token, str) or not token:
        raise InvalidToken("malformed")

    mat = _load()

    # Decide which key to verify against. If the token's header carries
    # a ``kid`` we look it up in {active, previous}. If it carries no
    # ``kid`` (legacy tokens issued before rotation support landed), we
    # fall back to the active key — that's the safe default and matches
    # the behaviour all existing tests rely on.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.DecodeError as exc:
        raise InvalidToken("malformed", underlying=exc) from exc
    candidate_kid = unverified_header.get("kid") if unverified_header else None

    public_pem = mat.public_pem
    if candidate_kid and candidate_kid != mat.fingerprint:
        prev = _load_previous()
        if prev is not None and prev.fingerprint == candidate_kid:
            public_pem = prev.public_pem
        # If neither key matches we still try the active key — PyJWT
        # will then raise InvalidSignatureError, which we map to
        # ``"signature"``. We deliberately do not 401 here on the
        # ``kid`` mismatch alone, because an attacker could otherwise
        # probe key fingerprints by varying the header.

    try:
        decoded = jwt.decode(
            token,
            public_pem,
            algorithms=[ALGORITHM],  # explicit list — DO NOT change to None
            issuer=TOKEN_ISSUER,
            leeway=leeway,
            options={
                "require": ["exp", "iat", "nbf", "iss"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidToken("expired", underlying=exc) from exc
    except jwt.ImmatureSignatureError as exc:
        raise InvalidToken("not_before", underlying=exc) from exc
    except jwt.InvalidIssuerError as exc:
        raise InvalidToken("issuer", underlying=exc) from exc
    except jwt.InvalidAlgorithmError as exc:
        raise InvalidToken("algorithm", underlying=exc) from exc
    except jwt.InvalidSignatureError as exc:
        raise InvalidToken("signature", underlying=exc) from exc
    except jwt.DecodeError as exc:
        # Covers malformed base64, bad headers, truncated tokens, and
        # — importantly — the attack where a bad actor strips the
        # signature and re-signs with "none". PyJWT raises DecodeError
        # on the latter because we passed ``algorithms=[ALGORITHM]``.
        raise InvalidToken("malformed", underlying=exc) from exc
    except jwt.InvalidTokenError as exc:
        # Catch-all for everything else PyJWT defines.
        raise InvalidToken("malformed", underlying=exc) from exc

    # Optional: if the caller passed an explicit ``now``, re-check
    # exp/nbf against it. PyJWT uses utcnow() internally so we can't
    # inject time, but doing a second pass gives tests deterministic
    # control. The leeway applies here too.
    if now is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        epoch = int(now.timestamp())
        exp = int(decoded["exp"])
        nbf = int(decoded["nbf"])
        if epoch > exp + leeway:
            raise InvalidToken("expired")
        if epoch + leeway < nbf:
            raise InvalidToken("not_before")

    return decoded


def jwks() -> Dict[str, Any]:
    """Return the JWKS dict (active + previous public keys, if any).

    Format follows RFC 7517. Each entry has:

    * ``kty``: ``"RSA"``
    * ``use``: ``"sig"``
    * ``alg``: ``"RS256"``
    * ``kid``: the 12-char fingerprint that matches the JWT header
    * ``n``, ``e``: base64url-encoded RSA modulus and exponent

    The control plane exposes this dict at ``/.well-known/jwks.json``.
    Agents fetch it on a 10-minute TTL and use the ``kid`` from each
    incoming token's header to pick the right key — that's how key
    rotation works without reimaging the field fleet.
    """
    import base64

    def _to_jwk(material: _KeyMaterial) -> Dict[str, Any]:
        # Parse the public PEM into a cryptography RSA public key so
        # we can read out the modulus + exponent. PyJWT has its own
        # PyJWK helper but it ships with extra deps; doing the encoding
        # ourselves keeps the import surface small.
        pub = serialization.load_pem_public_key(material.public_pem)
        if not isinstance(pub, rsa.RSAPublicKey):  # pragma: no cover
            raise SigningKeyUnavailable(
                "JWKS export only supports RSA keys"
            )
        numbers = pub.public_numbers()

        def _b64u(value: int) -> str:
            byte_len = (value.bit_length() + 7) // 8
            raw = value.to_bytes(byte_len, "big")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        return {
            "kty": "RSA",
            "use": "sig",
            "alg": ALGORITHM,
            "kid": material.fingerprint,
            "n": _b64u(numbers.n),
            "e": _b64u(numbers.e),
        }

    keys = [_to_jwk(_load())]
    prev = _load_previous()
    if prev is not None and prev.fingerprint != keys[0]["kid"]:
        keys.append(_to_jwk(prev))
    return {"keys": keys}


__all__ = [
    "ALGORITHM",
    "DEFAULT_LEEWAY_SECONDS",
    "InvalidToken",
    "SigningKeyUnavailable",
    "TOKEN_ISSUER",
    "jwks",
    "key_fingerprint",
    "public_key_pem",
    "reset_cache_for_tests",
    "sign",
    "verify",
]
