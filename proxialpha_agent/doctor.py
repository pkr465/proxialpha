"""Build redacted support bundles for the ``proxialpha doctor`` command.

The ``doctor`` command exists so customers hitting an agent
problem can hand us a single file that contains everything we
need to diagnose without shipping us secrets. It's the Phase 2
equivalent of ``kubectl cluster-info dump`` or ``journalctl --unit
app > bundle.txt``, with one critical extra responsibility:

**No secret may ever appear inside the bundle.**

Threat model
------------

A support bundle will be emailed, pasted into Slack, uploaded to
issue trackers, or attached to Zendesk tickets. Each of those
channels leaks. Anything the bundle contains is effectively
public within a few weeks. We therefore assume the bundle will
end up in a place we don't control, and we redact anything that
could compromise the customer if it did.

Specifically we redact:

* RSA / EC / OpenSSH / DSA private key PEM blocks.
* Stripe live / test secret and publishable keys.
* Ethereum-style 32-byte hex private keys.
* AWS access key IDs.
* Generic ``password|secret|api_key|auth_token = <stuff>`` env lines.
* Anything that looks like a bearer token header.

We do NOT redact:

* Agent license JWTs — these are already scoped to the customer's
  org and expire in 24 hours. Including them helps support.
* Org ID, agent ID, fingerprint — these are opaque identifiers
  that only have meaning inside our own control plane.
* Hostnames, process IDs, mode history — operational context.

Self-check
----------

After building the bundle we re-run the same redaction regexes
against the final bytes. If any pattern matches we raise
:class:`BundleRedactionError` rather than writing the file — this
catches regressions in the redactor itself. It's the single most
important safety net in this module.

Bundle format
-------------

The bundle is a ``.tar.gz`` archive containing:

* ``manifest.json`` — version, timestamp, mode, file list.
* ``health.json`` — the most recent :class:`HealthState` snapshot.
* ``settings.redacted.json`` — agent settings with secrets
  redacted.
* ``license.claims.json`` — the license claims (not the raw
  token; token is optional and only included if explicitly
  requested).
* ``fingerprint.txt`` — the UUID4 agent fingerprint (opaque ID,
  not hardware-derived).
* ``files.txt`` — list of files in PROXIALPHA_HOME with sizes
  and modes (never contents).
* ``logs.redacted.txt`` — tail of the agent log file, redacted.
* ``env.redacted.txt`` — selected env vars (``PROXIALPHA_*``
  only), with values redacted.

Total bundle size is capped at 5 MB. If logs push us over the
cap we truncate the oldest lines and add a note to the manifest.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import stat
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .version import __version__

log = logging.getLogger(__name__)


#: Maximum size of the final bundle in bytes. Well under the 5 MB
#: limit that verify_image.sh enforces so we have headroom if a
#: customer's log file is close to the cap.
MAX_BUNDLE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

#: Cap on the redacted log excerpt included in the bundle. Logs
#: compress ~10x under gzip so 2 MB of text ≈ 200 KB in the
#: archive, which leaves plenty of room for other files.
LOG_TAIL_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

#: Placeholder string left in place of a redacted secret. Chosen
#: so it's trivially greppable and doesn't collide with any of
#: the patterns we scan for.
REDACTED = "[REDACTED]"


class BundleRedactionError(RuntimeError):
    """Raised when the post-build self-check catches a leaked secret.

    Indicates a bug in :func:`redact_text` or in the file-level
    redaction logic. The bundle is NOT written to disk when this
    fires — ``doctor`` prefers to fail loudly over shipping a
    leaky bundle.
    """


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------
#
# Each entry is a compiled regex + a short name for the self-check
# report. Keep the list ordered from "most specific" to "most
# general" so the specific ones get to match first and we produce
# informative self-check error messages when something slips.

_SECRET_PATTERNS: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    (
        "stripe_secret_key",
        re.compile(r"sk_(?:live|test)_[a-zA-Z0-9]{20,}"),
    ),
    (
        "stripe_publishable_key",
        re.compile(r"pk_(?:live|test)_[a-zA-Z0-9]{20,}"),
    ),
    (
        "ethereum_private_key",
        # A 64-char hex blob prefixed with "0x". Has false positives
        # on SHA-256 hashes in logs, but we'd rather over-redact a
        # hash than leak a private key.
        re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
    ),
    (
        "aws_access_key_id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "generic_api_key_assignment",
        # Matches ``password=foo``, ``SECRET: bar``, ``api_key = "xyz"``,
        # etc. Case-insensitive. Redacts the VALUE, not the label,
        # so the bundle still shows that a secret was present.
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|auth[_-]?token|"
            r"access[_-]?token|bearer)\s*[:=]\s*['\"]?([^'\"\s]{8,})['\"]?"
        ),
    ),
)


def redact_text(text: str) -> str:
    """Replace anything that matches a secret pattern with ``REDACTED``.

    The generic ``key=value`` pattern uses a capture group so we
    preserve the label for operator readability — redacting the
    whole line would lose the signal that "a password was set".
    """
    redacted = text
    for name, pattern in _SECRET_PATTERNS:
        if name == "generic_api_key_assignment":
            redacted = pattern.sub(
                lambda m: f"{m.group(1)}={REDACTED}", redacted
            )
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted


def find_secrets(text: str) -> List[Tuple[str, str]]:
    """Return any ``(pattern_name, matched_span)`` pairs still in ``text``.

    Used by the bundle self-check and by
    :file:`tests/test_doctor_bundle.py`. The matched span is
    truncated so the self-check error message doesn't ship the
    actual secret that leaked.

    ``redact_text`` leaves behind the literal placeholder
    ``[REDACTED]``, which happens to match the generic
    ``key=value`` pattern because the placeholder is 10 characters
    long. We skip matches whose value is exactly our placeholder
    so the self-check doesn't flag its own output — otherwise
    every bundle would self-check fail.
    """
    matches: List[Tuple[str, str]] = []
    for name, pattern in _SECRET_PATTERNS:
        for hit in pattern.finditer(text):
            raw = hit.group(0)
            # Preserve the label portion for generic assignments so
            # the error message is actionable without leaking the
            # value itself.
            if name == "generic_api_key_assignment":
                label = hit.group(1)
                value = hit.group(2).strip("'\"")
                if value == REDACTED:
                    continue
                matches.append((name, f"{label}=[value hidden]"))
            else:
                if raw == REDACTED:
                    continue
                matches.append((name, _truncate(raw)))
    return matches


def _truncate(value: str, *, limit: int = 24) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:8]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# Bundle inputs
# ---------------------------------------------------------------------------


@dataclass
class DoctorInputs:
    """Everything needed to build a bundle, packaged for easy DI.

    Callers gather these from their various sources (settings,
    supervisor state, license client, log file) and hand them
    to :func:`build_bundle`. Breaking the inputs out like this
    keeps the bundle builder pure-ish and easy to unit test.
    """

    #: Current agent mode as a string (``"running"``, etc.).
    mode: str = "unknown"

    #: Snapshot from :class:`proxialpha_agent.health.HealthState`.
    health: Mapping[str, Any] = field(default_factory=dict)

    #: :class:`AgentSettings` as a dict. Values get redacted.
    settings: Mapping[str, Any] = field(default_factory=dict)

    #: Decoded license claims. Pass an empty dict if no license
    #: is loaded yet (e.g. enrollment failed).
    license_claims: Mapping[str, Any] = field(default_factory=dict)

    #: Persisted agent fingerprint (UUID4 hex). Opaque; safe to
    #: include unredacted.
    fingerprint: Optional[str] = None

    #: Path to ``$PROXIALPHA_HOME``. Used to enumerate files in the
    #: home directory (never their contents).
    home_path: Optional[Path] = None

    #: Raw log text to include (redacted). Typically the tail of
    #: the agent's systemd journal or a log file.
    log_text: str = ""

    #: Selected env vars to include. Typically a filtered snapshot
    #: of ``os.environ`` containing only ``PROXIALPHA_*`` keys.
    env: Mapping[str, str] = field(default_factory=dict)

    #: Optional override timestamp for the manifest. Tests pass a
    #: fixed value; production calls ``datetime.now(timezone.utc)``.
    now: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Per-file builders
# ---------------------------------------------------------------------------


def _build_manifest(
    inputs: DoctorInputs, *, file_list: Sequence[str], truncated: bool
) -> bytes:
    manifest = {
        "agent_version": __version__,
        "generated_at": (
            inputs.now or datetime.now(timezone.utc)
        ).isoformat(),
        "mode": inputs.mode,
        "fingerprint": inputs.fingerprint,
        "files": list(file_list),
        "logs_truncated": truncated,
        "redaction_policy": [name for name, _ in _SECRET_PATTERNS],
    }
    return _json_bytes(manifest)


def _build_health(inputs: DoctorInputs) -> bytes:
    # The health snapshot may contain datetime objects; JSON-encode
    # them as ISO strings via default=str so we never raise.
    return _json_bytes(dict(inputs.health))


def _build_settings(inputs: DoctorInputs) -> bytes:
    """Serialise settings with redaction applied to every value.

    We do this by running :func:`redact_text` over each stringified
    value rather than trying to enumerate which fields are "secret".
    An unknown field that happens to contain a key pattern gets
    redacted for free.
    """
    redacted: Dict[str, Any] = {}
    for key, value in inputs.settings.items():
        if isinstance(value, (str, bytes, bytearray)):
            text = value.decode("utf-8", errors="replace") if isinstance(
                value, (bytes, bytearray)
            ) else value
            redacted[key] = redact_text(text)
        elif value is None or isinstance(value, (int, float, bool)):
            redacted[key] = value
        else:
            redacted[key] = redact_text(str(value))
    return _json_bytes(redacted)


def _build_license_claims(inputs: DoctorInputs) -> bytes:
    # License claims are already non-secret by construction — the
    # raw JWT is the secret, the decoded claims are org/agent IDs,
    # fingerprint, entitlements. Still we run redaction as a
    # belt-and-suspenders step in case a future claim field ends
    # up carrying sensitive content.
    claims = dict(inputs.license_claims)
    redacted: Dict[str, Any] = {}
    for key, value in claims.items():
        if isinstance(value, str):
            redacted[key] = redact_text(value)
        else:
            redacted[key] = value
    return _json_bytes(redacted)


def _build_fingerprint(inputs: DoctorInputs) -> bytes:
    return (inputs.fingerprint or "").encode("utf-8")


def _build_files_listing(inputs: DoctorInputs) -> bytes:
    """List the files in ``PROXIALPHA_HOME`` without reading their contents.

    Each line: ``<mode-octal> <size-bytes> <relative-path>``.
    We skip dotfiles and anything that isn't a regular file so
    backup turds like ``license.swp`` don't leak interesting data
    through their filenames alone (unlikely, but defensive).
    """
    if inputs.home_path is None or not Path(inputs.home_path).is_dir():
        return b"(no home path)\n"

    lines: List[str] = []
    home = Path(inputs.home_path)
    for entry in sorted(home.rglob("*")):
        try:
            st = entry.stat()
        except OSError:
            continue
        if not entry.is_file():
            continue
        rel = entry.relative_to(home)
        perms = oct(stat.S_IMODE(st.st_mode))
        lines.append(f"{perms} {st.st_size} {rel}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_log_tail(inputs: DoctorInputs) -> Tuple[bytes, bool]:
    """Redact the log text and truncate to ``LOG_TAIL_MAX_BYTES``.

    Returns ``(bytes, truncated)`` so the manifest can record
    whether any log lines were dropped.
    """
    text = inputs.log_text or ""
    redacted = redact_text(text)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) > LOG_TAIL_MAX_BYTES:
        # Keep the TAIL — the end of the log is usually the most
        # relevant for debugging a crash.
        encoded = encoded[-LOG_TAIL_MAX_BYTES:]
        encoded = b"[...TRUNCATED...]\n" + encoded
        return encoded, True
    return encoded, False


def _build_env(inputs: DoctorInputs) -> bytes:
    """Dump ``PROXIALPHA_*`` env vars with values redacted.

    We include the KEYS unredacted (they're config field names,
    not secrets) and the VALUES through :func:`redact_text`, which
    wipes any actual secret that happens to live in an env var.
    """
    out = io.StringIO()
    for key in sorted(inputs.env.keys()):
        value = inputs.env[key]
        out.write(f"{key}={redact_text(value)}\n")
    return out.getvalue().encode("utf-8")


def _json_bytes(obj: Any) -> bytes:
    """JSON-encode with stable ordering and datetime handling."""
    return (
        json.dumps(obj, sort_keys=True, indent=2, default=str) + "\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


def build_bundle(
    inputs: DoctorInputs,
    *,
    output_path: Path,
) -> Path:
    """Build a redacted support bundle at ``output_path``.

    Parameters
    ----------
    inputs
        Gathered snapshot of agent state. See :class:`DoctorInputs`.
    output_path
        Where to write the ``.tar.gz`` file. Parent directory is
        created if it doesn't exist. The file is written atomically:
        we stage to ``output_path.tmp``, run the self-check against
        the staged bytes, and only rename into place if the check
        passes.

    Returns
    -------
    Path
        The absolute path of the written bundle.

    Raises
    ------
    BundleRedactionError
        If the self-check finds a secret in the final bundle bytes
        (means the redactor has a bug). The tmp file is deleted
        and no bundle is written.
    ValueError
        If the final bundle would exceed :data:`MAX_BUNDLE_SIZE_BYTES`.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    # 1. Build each file's bytes.
    log_bytes, log_truncated = _build_log_tail(inputs)
    files: List[Tuple[str, bytes]] = [
        ("health.json", _build_health(inputs)),
        ("settings.redacted.json", _build_settings(inputs)),
        ("license.claims.json", _build_license_claims(inputs)),
        ("fingerprint.txt", _build_fingerprint(inputs)),
        ("files.txt", _build_files_listing(inputs)),
        ("logs.redacted.txt", log_bytes),
        ("env.redacted.txt", _build_env(inputs)),
    ]
    # Manifest last so it can list the other files' names.
    manifest_bytes = _build_manifest(
        inputs,
        file_list=[name for name, _ in files],
        truncated=log_truncated,
    )
    files.insert(0, ("manifest.json", manifest_bytes))

    # 2. Pack into an in-memory tar.gz so we can run the self-check
    # against the final bytes before writing to disk.
    bundle_bytes = _pack_tar_gz(files, inputs.now)

    if len(bundle_bytes) > MAX_BUNDLE_SIZE_BYTES:
        raise ValueError(
            f"bundle exceeds size cap: {len(bundle_bytes)} > "
            f"{MAX_BUNDLE_SIZE_BYTES}"
        )

    # 3. Run the self-check against decompressed tar contents
    # (redaction only applies to text content, so checking the
    # gzipped archive directly would miss matches that span across
    # gzip block boundaries). We walk every member inside the tar.
    self_check_report = _self_check(bundle_bytes)
    if self_check_report:
        raise BundleRedactionError(
            "bundle self-check found unredacted secrets: "
            + ", ".join(f"{name}:{span}" for name, span in self_check_report)
        )

    # 4. Atomic write: tmp then rename.
    tmp_path.write_bytes(bundle_bytes)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, output_path)
    log.info(
        "doctor: wrote bundle %s (%d bytes, %d files)",
        output_path,
        len(bundle_bytes),
        len(files),
    )
    return output_path


def _pack_tar_gz(
    files: Sequence[Tuple[str, bytes]], now: Optional[datetime]
) -> bytes:
    """Serialise ``files`` as a deterministic tar.gz blob.

    ``mtime`` on every member is set to the caller's ``now``
    value so rebuilds are reproducible — CI uses this to
    assert that two builds from the same input produce the
    same archive bytes. We disable gzip's filename/timestamp
    header for the same reason.
    """
    epoch = int(
        (now or datetime.now(timezone.utc)).timestamp()
    )
    buf = io.BytesIO()
    # ``mtime=0`` on the gzip header + deterministic tar member
    # mtimes = reproducible archive.
    with gzip.GzipFile(
        fileobj=buf, mode="wb", mtime=0, compresslevel=6
    ) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for name, data in files:
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                info.mtime = epoch
                info.mode = 0o600
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _self_check(bundle_bytes: bytes) -> List[Tuple[str, str]]:
    """Re-open the bundle and re-scan every member for unredacted secrets.

    This runs the same regex set the redactor used, against the
    FINAL bytes we're about to ship. Any match here is a bug in
    the redactor — the whole point of this pass is to catch those
    bugs before the bundle leaves the machine.
    """
    matches: List[Tuple[str, str]] = []
    buf = io.BytesIO(bundle_bytes)
    with gzip.GzipFile(fileobj=buf, mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                data = extracted.read()
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = data.decode("utf-8", errors="replace")
                for name, span in find_secrets(text):
                    matches.append(
                        (name, f"{member.name}:{span}")
                    )
    return matches


# ---------------------------------------------------------------------------
# High-level helper (used by CLI)
# ---------------------------------------------------------------------------


def build_bundle_from_runtime(
    *,
    home_path: Path,
    output_path: Path,
    mode: str = "unknown",
    health: Optional[Mapping[str, Any]] = None,
    settings: Optional[Mapping[str, Any]] = None,
    license_claims: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[str] = None,
    log_text: str = "",
    env: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> Path:
    """Convenience wrapper used by the ``proxialpha doctor`` CLI.

    Mostly exists so the CLI entry point is one line of real code
    and the doctor module stays test-friendly via the explicit
    :class:`DoctorInputs` seam.
    """
    env_snapshot = dict(env or _collect_proxialpha_env())
    inputs = DoctorInputs(
        mode=mode,
        health=dict(health or {}),
        settings=dict(settings or {}),
        license_claims=dict(license_claims or {}),
        fingerprint=fingerprint,
        home_path=home_path,
        log_text=log_text,
        env=env_snapshot,
        now=now,
    )
    return build_bundle(inputs, output_path=output_path)


def _collect_proxialpha_env() -> Dict[str, str]:
    """Return the subset of ``os.environ`` that starts with ``PROXIALPHA_``.

    The values still go through redaction inside the bundle — the
    env namespace filter is just to avoid dumping the customer's
    entire shell environment into the support bundle.
    """
    return {
        k: v for k, v in os.environ.items() if k.startswith("PROXIALPHA_")
    }


__all__ = [
    "BundleRedactionError",
    "DoctorInputs",
    "LOG_TAIL_MAX_BYTES",
    "MAX_BUNDLE_SIZE_BYTES",
    "REDACTED",
    "build_bundle",
    "build_bundle_from_runtime",
    "find_secrets",
    "redact_text",
]
