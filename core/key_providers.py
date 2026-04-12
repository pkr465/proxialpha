"""Pluggable signing-key providers (P1-5).

The control plane signs every agent license JWT with an RS256 private
key. Phase 1 hard-coded the key resolution path inside
:mod:`core.jwt_keys` (env-var → PEM file → dev fallback). That's fine
for a single-pod dev deploy but doesn't compose with the real-world
production options operators want:

* **AWS KMS** — sign through the KMS API; the private key never
  leaves the HSM. Requires the IAM role to allow ``kms:Sign``.
* **GCP KMS** — same shape, different SDK.
* **HashiCorp Vault** — Vault holds the key; we ask Vault to sign on
  our behalf.
* **File** — the existing path, useful for staging and dev.

This module defines the **provider interface** so :mod:`core.jwt_keys`
can dispatch to whichever one is configured by
``settings.signing_key_provider`` without growing a giant if/else.
The current concrete implementation is :class:`FileKeyProvider`,
which preserves the legacy environment-variable resolution. KMS and
Vault providers are sketched out as stubs that raise a clear
``NotImplementedError`` so an operator who wires them up gets a
deterministic error pointing to the runbook, not a silent fallback
to the file path.

Why an interface instead of inlining the dispatch?
--------------------------------------------------

Three reasons:

1. **Tests can stub a provider** without touching the env. The
   existing test suite uses ``reset_cache_for_tests`` and patches
   env vars; the new shape gives test fixtures a clean injection
   point that doesn't have to know about disk paths.

2. **The abstract surface documents the contract** any future KMS
   integration must satisfy. Right now there are several "what does
   the KMS path even look like?" questions buried in the rotation
   runbook. Codifying them as an ABC makes the answer the code.

3. **Migration safety**. When the KMS provider lands, it lands as a
   new subclass — the file provider stays unchanged and the
   ``signing_key_provider`` flag flips at the env-var level only.
   Zero risk of cross-contaminating the working dev path.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class KeyMaterial:
    """The bytes a provider must return when asked for a key.

    ``private_pem`` is the PKCS#8-encoded RSA private key. ``public_pem``
    is the SubjectPublicKeyInfo public key. ``fingerprint`` is a 12-char
    SHA-256 hex prefix used for both log lines and the JWT header
    ``kid``. ``source`` is a free-form string for diagnostics — log
    lines print it at startup so an operator can tell at a glance
    which provider booted the process.
    """

    private_pem: bytes
    public_pem: bytes
    fingerprint: str
    source: str


class SigningKeyProvider(ABC):
    """Abstract base for anything that can hand us a signing key.

    Implementations are constructed once at process startup and may
    cache state internally — the wrapper in :mod:`core.jwt_keys`
    only calls :meth:`load_active` once per cold start and
    :meth:`load_previous` once per JWKS rebuild.
    """

    @abstractmethod
    def load_active(self) -> Optional[KeyMaterial]:
        """Return the **current** signing key, or ``None`` if unset.

        Returning ``None`` is the dev-only escape hatch — the wrapper
        will refuse to start in ``ENV=prod`` if the provider returns
        nothing. Real providers should raise instead so the operator
        sees a stack trace pointing at the misconfiguration.
        """

    @abstractmethod
    def load_previous(self) -> Optional[KeyMaterial]:
        """Return the **previous** key for JWKS overlap during rotation.

        Returning ``None`` (the steady state) means JWKS publishes
        only the active key.
        """


class FileKeyProvider(SigningKeyProvider):
    """The legacy path: load the key from a PEM file or env var.

    Resolution order matches the pre-P1-5 behaviour exactly so an
    upgrade-in-place doesn't change which key the process boots
    with:

    1. ``AGENT_SIGNING_KEY_PATH`` — path to a PKCS#8 PEM file.
    2. ``AGENT_SIGNING_KEY_PEM`` — raw PEM in an env var (for K8s
       Secrets that mount as env).

    The previous-key resolution mirrors this with the
    ``AGENT_PREVIOUS_*`` env-var prefix.

    The actual PEM-parsing logic still lives in :mod:`core.jwt_keys`
    via ``_materialize`` so we don't duplicate the cryptography
    surface here. This class is just the *resolver* — it returns
    raw bytes and a tag, and the caller materialises them.
    """

    ACTIVE_PATH_ENV = "AGENT_SIGNING_KEY_PATH"
    ACTIVE_PEM_ENV = "AGENT_SIGNING_KEY_PEM"
    PREV_PATH_ENV = "AGENT_PREVIOUS_SIGNING_KEY_PATH"
    PREV_PEM_ENV = "AGENT_PREVIOUS_SIGNING_KEY_PEM"

    def load_active(self) -> Optional[KeyMaterial]:
        return self._load_pair(self.ACTIVE_PATH_ENV, self.ACTIVE_PEM_ENV, "active")

    def load_previous(self) -> Optional[KeyMaterial]:
        return self._load_pair(self.PREV_PATH_ENV, self.PREV_PEM_ENV, "previous")

    @staticmethod
    def _load_pair(
        path_env: str, pem_env: str, tag: str
    ) -> Optional[KeyMaterial]:
        path = os.environ.get(path_env)
        if path:
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"{path_env}={path!r} does not point to a readable file"
                )
            with open(path, "rb") as fh:
                priv_pem = fh.read()
            return KeyMaterial(
                private_pem=priv_pem,
                public_pem=b"",  # filled by jwt_keys._materialize
                fingerprint="",
                source=f"file:{path_env}",
            )
        raw = os.environ.get(pem_env)
        if raw:
            priv_pem = raw.encode("utf-8") if isinstance(raw, str) else raw
            return KeyMaterial(
                private_pem=priv_pem,
                public_pem=b"",
                fingerprint="",
                source=f"file:{pem_env}",
            )
        return None


class KMSKeyProviderStub(SigningKeyProvider):
    """Placeholder for a real KMS-backed provider.

    Subclasses (``AWSKMSKeyProvider``, ``GCPKMSKeyProvider``,
    ``VaultKeyProvider``) will live in their own modules so the
    cloud SDK imports stay optional. Right now the abstract class
    raises a clear error so an operator who flips the
    ``signing_key_provider`` setting before the implementation
    lands knows exactly what to do next.
    """

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name

    def load_active(self) -> KeyMaterial:
        raise NotImplementedError(
            f"{self.provider_name} signing key provider is not yet "
            f"implemented. See docs/runbooks/signing-key-rotation.md "
            f"§KMS for the migration plan, or set "
            f"SIGNING_KEY_PROVIDER=file as a temporary workaround."
        )

    def load_previous(self) -> Optional[KeyMaterial]:
        return None


def get_provider(name: Optional[str]) -> SigningKeyProvider:
    """Construct the provider matching ``name`` (or ``"file"`` by default).

    The accepted values mirror ``settings.signing_key_provider``:
    ``"file"``, ``"aws-kms"``, ``"gcp-kms"``, ``"vault"``. Anything
    else falls back to the file provider with a warning so a typo
    doesn't strand the process at startup.
    """
    canonical = (name or "file").strip().lower()
    if canonical == "file":
        return FileKeyProvider()
    if canonical in ("aws-kms", "gcp-kms", "vault"):
        return KMSKeyProviderStub(provider_name=canonical)
    log.warning(
        "key_providers: unknown signing_key_provider=%r; falling back to file",
        name,
    )
    return FileKeyProvider()


__all__ = [
    "FileKeyProvider",
    "KMSKeyProviderStub",
    "KeyMaterial",
    "SigningKeyProvider",
    "get_provider",
]
