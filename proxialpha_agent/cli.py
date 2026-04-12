"""Console-script entry point for the ProxiAlpha agent.

The ``proxialpha`` command in ``pyproject.toml`` points here. It
provides a tiny subcommand router so operators on customer hosts
can drive the agent without reaching into Python:

* ``proxialpha run`` — start the supervisor and run the heartbeat
  loop. This is what the systemd unit / Docker CMD invokes.
* ``proxialpha enroll --install-token <TOKEN>`` — one-shot
  enrollment. Normally ``run`` handles this implicitly, but the
  standalone command is useful when the dashboard gives the user
  a token and they want to verify the agent can talk to the
  control plane before flipping on the systemd unit.
* ``proxialpha version`` — print the agent version. Used by
  support to confirm what the customer is actually running.
* ``proxialpha check`` — load the on-disk license and print its
  claims. Does not hit the network. Useful for debugging "why
  won't my agent start?".
* ``proxialpha doctor --output <PATH>`` — build a redacted
  support bundle (``.tar.gz``) that customers can send to
  support. Gathers settings, license claims, fingerprint, a
  listing of ``$PROXIALPHA_HOME``, a log tail, and filtered env
  vars; runs everything through a secret-redaction pass; runs a
  post-build self-check against the final bundle bytes; writes
  the file atomically at ``0600``. See :mod:`proxialpha_agent.doctor`
  for the full threat model.

Design notes
------------

* The router is hand-rolled rather than using ``click`` or
  ``typer`` because we want zero additional runtime deps on
  customer hosts. ``argparse`` is stdlib.
* Every subcommand returns an ``int`` exit code rather than
  calling ``sys.exit`` itself — :func:`main` does the exit so
  tests can call individual subcommands and inspect their
  return value.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .license import LicenseClient, LicenseError
from .settings import AgentSettings, get_settings

log = logging.getLogger(__name__)


def _configure_logging(level_name: str) -> None:
    """Install a JSON-per-line root handler.

    The agent runs headless on customer boxes; structured logs
    make it easy for the customer's own log pipeline (Datadog,
    CloudWatch, etc.) to pick up events without a custom parser.
    We stay on stdlib logging — no structlog dependency — to
    keep the install footprint small.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)

    class _JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload = {
                "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["exc_info"] = self.formatException(record.exc_info)
            return json.dumps(payload, default=str)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    # Remove any handlers installed by a previous configure call
    # (important in tests that call main() multiple times).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)


def _load_public_key_pem(settings: AgentSettings) -> bytes:
    """Resolve the control-plane public key PEM.

    The operator may supply the key path explicitly via
    ``PROXIALPHA_PUBLIC_KEY_PATH``, in which case we read it
    directly. Otherwise we fall back to the bundled
    ``keys/dev_pub.pem`` shipped inside the package.
    """
    if settings.public_key_path is not None:
        return Path(settings.public_key_path).read_bytes()

    bundled = Path(__file__).parent / "keys" / "dev_pub.pem"
    if not bundled.exists():
        raise FileNotFoundError(
            f"no public key found; set PROXIALPHA_PUBLIC_KEY_PATH or install "
            f"a dev key at {bundled}"
        )
    return bundled.read_bytes()


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_version(_args: argparse.Namespace) -> int:
    """Print the agent version and exit 0."""
    print(f"proxialpha-agent {__version__}")
    return 0


def _cmd_check(_args: argparse.Namespace) -> int:
    """Load and print the on-disk license claims without hitting the network."""
    settings = get_settings()
    try:
        pem = _load_public_key_pem(settings)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = LicenseClient(
        public_key_pem=pem,
        license_path=settings.license_path,
        fingerprint_path=settings.fingerprint_path,
    )
    try:
        license_obj = client.load_from_disk()
    except LicenseError as exc:
        print(
            f"license invalid: reason={exc.reason} message={exc}",
            file=sys.stderr,
        )
        return 1

    claims = {
        "agent_id": license_obj.agent_id,
        "org_id": license_obj.org_id,
        "fingerprint": license_obj.fingerprint,
        "issued_at": str(license_obj.issued_at),
        "not_before": str(license_obj.not_before),
        "expires_at": str(license_obj.expires_at),
        "grace_until": str(license_obj.grace_until),
    }
    print(json.dumps(claims, indent=2, default=str))
    return 0


def _cmd_enroll(args: argparse.Namespace) -> int:
    """One-shot enrollment against the control plane."""
    settings = get_settings()
    token = args.install_token or settings.install_token
    if not token:
        print(
            "error: --install-token is required (or set "
            "PROXIALPHA_INSTALL_TOKEN)",
            file=sys.stderr,
        )
        return 2
    try:
        pem = _load_public_key_pem(settings)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = LicenseClient(
        public_key_pem=pem,
        license_path=settings.license_path,
        fingerprint_path=settings.fingerprint_path,
    )
    try:
        license_obj = client.enroll(
            install_token=token,
            control_plane_url=settings.control_plane_url,
        )
    except LicenseError as exc:
        print(
            f"enroll failed: reason={exc.reason} message={exc}",
            file=sys.stderr,
        )
        return 1
    print(f"enrolled: agent_id={license_obj.agent_id} org_id={license_obj.org_id}")
    return 0


def _cmd_run(_args: argparse.Namespace) -> int:
    """Start the supervisor and run the heartbeat loop until shutdown."""
    # Imported lazily so ``proxialpha version`` doesn't pay the cost
    # of importing asyncio + httpx + supervisor on every CLI call.
    from .__main__ import run_agent  # noqa: WPS433 — intentional lazy import

    return asyncio.run(run_agent())


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Build a redacted support bundle at ``args.output``.

    The doctor bundle is security-critical: it ships across
    untrusted channels (email, Slack, Zendesk) and must never
    contain secrets. :func:`proxialpha_agent.doctor.build_bundle`
    runs a post-build self-check against the final bytes and
    refuses to write the file if any secret regex still matches.

    We keep this command resilient: if the license on disk is
    malformed we still include everything else, so support can
    diagnose precisely that failure mode. Likewise, a missing log
    file is not fatal — we just ship an empty log section.
    """
    # Imported lazily so the cheap ``proxialpha version`` path
    # doesn't pay for pulling in the tarfile/gzip/regex stack.
    from .doctor import build_bundle_from_runtime  # noqa: WPS433

    settings = get_settings()

    # Try to load license claims, but don't fail the whole command
    # if the license is missing / broken — that's often WHY the user
    # is running doctor in the first place.
    license_claims: dict = {}
    fingerprint: Optional[str] = None
    try:
        pem = _load_public_key_pem(settings)
        client = LicenseClient(
            public_key_pem=pem,
            license_path=settings.license_path,
            fingerprint_path=settings.fingerprint_path,
        )
        fingerprint = client.fingerprint()
        try:
            lic = client.load_from_disk()
            license_claims = {
                "agent_id": lic.agent_id,
                "org_id": lic.org_id,
                "fingerprint": lic.fingerprint,
                "entitlements_snapshot": dict(lic.entitlements_snapshot),
                "issued_at": str(lic.issued_at),
                "not_before": str(lic.not_before),
                "expires_at": str(lic.expires_at),
                "grace_until": str(lic.grace_until),
            }
        except LicenseError as exc:
            license_claims = {
                "error": f"{exc.reason}: {exc}",
            }
    except FileNotFoundError as exc:
        license_claims = {"error": f"public-key: {exc}"}

    # Optional log tail.
    log_text = ""
    if args.log_file:
        try:
            log_text = Path(args.log_file).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            log_text = f"(failed to read log file {args.log_file!r}: {exc})"

    # Serialise settings through ``model_dump`` — pydantic handles
    # Path/Optional/SecretStr etc. and the bundle builder then
    # redacts every stringified value.
    try:
        settings_snapshot = settings.model_dump()
    except Exception:  # pragma: no cover — defensive
        settings_snapshot = {}

    try:
        path = build_bundle_from_runtime(
            home_path=settings.home,
            output_path=Path(args.output),
            mode="unknown",  # CLI invocation has no supervisor context
            settings=settings_snapshot,
            license_claims=license_claims,
            fingerprint=fingerprint,
            log_text=log_text,
        )
    except Exception as exc:
        print(f"doctor: failed to build bundle: {exc}", file=sys.stderr)
        return 1

    print(f"wrote support bundle: {path}")
    return 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxialpha",
        description="ProxiAlpha customer agent — license, heartbeat, health.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run the supervisor and heartbeat loop.")
    sub.add_parser("version", help="Print the agent version.")
    sub.add_parser("check", help="Verify the on-disk license and print claims.")

    enroll = sub.add_parser("enroll", help="One-shot enrollment.")
    enroll.add_argument(
        "--install-token",
        default=None,
        help=(
            "One-shot install token from the dashboard. If omitted, "
            "falls back to the PROXIALPHA_INSTALL_TOKEN env var."
        ),
    )

    doctor = sub.add_parser(
        "doctor",
        help="Build a redacted support bundle (.tar.gz) for support.",
    )
    doctor.add_argument(
        "--output",
        "-o",
        required=True,
        help="Where to write the support bundle (.tar.gz path).",
    )
    doctor.add_argument(
        "--log-file",
        default=None,
        help=(
            "Optional path to an agent log file whose tail should "
            "be redacted and included in the bundle."
        ),
    )

    return parser


_DISPATCH = {
    "run": _cmd_run,
    "version": _cmd_version,
    "check": _cmd_check,
    "enroll": _cmd_enroll,
    "doctor": _cmd_doctor,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns an exit code; caller decides whether to exit."""
    settings = get_settings()
    _configure_logging(settings.log_level)
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.command]
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main"]
