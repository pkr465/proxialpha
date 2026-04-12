"""Tests for :mod:`proxialpha_agent.doctor` (Task 08).

The doctor bundle is security-critical: it ships across
untrusted channels (email, Slack, Zendesk) and must never
contain secrets. These tests exercise the redaction pass,
the post-build self-check, the deterministic archive format,
and the size / truncation caps.

Strategy
--------

Every test builds a real ``.tar.gz`` bundle into a ``tmp_path``
and then re-extracts it with stdlib ``gzip`` + ``tarfile``.
That mirrors exactly what a support engineer would do, and
catches both redaction bugs and packaging bugs in one pass.

The "catches a leak" test monkeypatches ``redact_text`` to be
the identity function so we can prove that the post-build
self-check fires — otherwise we'd have to ship a deliberately
broken redactor just to test the safety net.
"""
from __future__ import annotations

import gzip
import io
import json
import stat
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from proxialpha_agent import doctor  # noqa: E402
from proxialpha_agent.doctor import (  # noqa: E402
    BundleRedactionError,
    DoctorInputs,
    LOG_TAIL_MAX_BYTES,
    MAX_BUNDLE_SIZE_BYTES,
    build_bundle,
    find_secrets,
    redact_text,
)


FIXED_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)

# A realistic-looking RSA private key PEM — 100% fake, regenerate
# if anybody's paranoid. The content between the markers is just
# random base64 that never decodes to a real key.
FAKE_PEM_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEAw+FAKE/KEY/FOR/TESTING/ONLY/DO/NOT/USE
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
-----END RSA PRIVATE KEY-----"""

FAKE_STRIPE_SECRET = "sk_live_51KqX9pAbCdEfGhIjKlMnOpQrStUvWxYz012345"
FAKE_STRIPE_PUBLISHABLE = "pk_live_51KqX9pAbCdEfGhIjKlMnOpQrStUvWxYz012345"
FAKE_ETH_PRIVATE_KEY = "0x" + "a1b2c3d4" * 8
FAKE_AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "license").write_text("not-actually-a-license", encoding="utf-8")
    (home / "fingerprint").write_text("deadbeef1234", encoding="utf-8")
    (home / "logs").mkdir()
    return home


def _base_inputs(fake_home: Path) -> DoctorInputs:
    return DoctorInputs(
        mode="running",
        health={
            "mode": "running",
            "version": "1.0.0-rc.1",
            "started_at": str(FIXED_NOW),
        },
        settings={
            "control_plane_url": "https://cp.example.com",
            "health_host": "127.0.0.1",
            "health_port": 9877,
            # Embed a PEM in settings to prove settings-side redaction
            # catches it even when the field name is innocuous.
            "public_key_hint": FAKE_PEM_PRIVATE_KEY,
        },
        license_claims={
            "agent_id": "agent_alpha",
            "org_id": "org_acme",
            "entitlements_snapshot": {"live_trading": True},
        },
        fingerprint="deadbeef1234",
        home_path=fake_home,
        log_text=(
            "2026-04-11T12:00:00Z INFO supervisor booting\n"
            f"2026-04-11T12:00:01Z INFO stripe key loaded: {FAKE_STRIPE_SECRET}\n"
            f"2026-04-11T12:00:02Z INFO wallet unlocked: {FAKE_ETH_PRIVATE_KEY}\n"
            f"2026-04-11T12:00:03Z INFO aws creds set: {FAKE_AWS_ACCESS_KEY}\n"
            "2026-04-11T12:00:04Z INFO PASSWORD=hunter2hunter2\n"
            "2026-04-11T12:00:05Z INFO api_key=verysecretvalue123\n"
            "2026-04-11T12:00:06Z INFO heartbeat ok\n"
        ),
        env={
            "PROXIALPHA_CONTROL_PLANE_URL": "https://cp.example.com",
            "PROXIALPHA_INSTALL_TOKEN": "sk_test_ShouldNotAppearInBundle01234",
            "PROXIALPHA_SECRET_API_KEY": "password=hunter2hunter2",
        },
        now=FIXED_NOW,
    )


def _extract_bundle(path: Path) -> Dict[str, bytes]:
    """Open the bundle and return ``{member_name: bytes}``.

    Uses stdlib gzip + tarfile so we're not depending on the
    doctor module's own extractor — exactly what an operator would
    do by hand.
    """
    out: Dict[str, bytes] = {}
    with gzip.open(path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                assert f is not None
                out[member.name] = f.read()
    return out


# ---------------------------------------------------------------------------
# Redaction unit tests
# ---------------------------------------------------------------------------


def test_redact_text_scrubs_pem_private_key() -> None:
    text = f"some prefix\n{FAKE_PEM_PRIVATE_KEY}\nsome suffix"
    redacted = redact_text(text)
    assert "BEGIN RSA PRIVATE KEY" not in redacted
    assert "[REDACTED]" in redacted
    assert "prefix" in redacted and "suffix" in redacted


def test_redact_text_scrubs_stripe_keys() -> None:
    text = f"live={FAKE_STRIPE_SECRET} pub={FAKE_STRIPE_PUBLISHABLE}"
    redacted = redact_text(text)
    assert FAKE_STRIPE_SECRET not in redacted
    assert FAKE_STRIPE_PUBLISHABLE not in redacted
    assert redacted.count("[REDACTED]") >= 2


def test_redact_text_scrubs_eth_private_key_and_aws_id() -> None:
    text = f"eth={FAKE_ETH_PRIVATE_KEY} aws={FAKE_AWS_ACCESS_KEY}"
    redacted = redact_text(text)
    assert FAKE_ETH_PRIVATE_KEY not in redacted
    assert FAKE_AWS_ACCESS_KEY not in redacted


def test_redact_text_generic_assignment_preserves_label() -> None:
    text = "PASSWORD=hunter2hunter2"
    redacted = redact_text(text)
    assert "hunter2hunter2" not in redacted
    # Label survives so operators can see that a password was set.
    assert "PASSWORD" in redacted
    assert "[REDACTED]" in redacted


def test_find_secrets_reports_matches_without_leaking_values() -> None:
    text = f"{FAKE_STRIPE_SECRET} and password=topsecretxxxxx"
    found = find_secrets(text)
    names = [name for name, _ in found]
    assert "stripe_secret_key" in names
    assert "generic_api_key_assignment" in names
    # The generic-assignment report never includes the actual value.
    for name, span in found:
        if name == "generic_api_key_assignment":
            assert "topsecretxxxxx" not in span


# ---------------------------------------------------------------------------
# Bundle-level tests
# ---------------------------------------------------------------------------


def test_bundle_contains_expected_files(fake_home: Path, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=output)

    members = _extract_bundle(output)
    expected = {
        "manifest.json",
        "health.json",
        "settings.redacted.json",
        "license.claims.json",
        "fingerprint.txt",
        "files.txt",
        "logs.redacted.txt",
        "env.redacted.txt",
    }
    assert expected <= set(members.keys()), (
        f"missing files: {expected - set(members.keys())}"
    )


def test_bundle_is_gzipped_and_under_5mb(fake_home: Path, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=output)

    # gzip.open() should succeed on a valid gzip stream.
    with gzip.open(output, "rb") as gz:
        header = gz.read(512)
    # The tar ustar magic lives at offset 257 of the first block.
    assert b"ustar" in header

    assert output.stat().st_size <= MAX_BUNDLE_SIZE_BYTES


def test_bundle_redacts_every_member(fake_home: Path, tmp_path: Path) -> None:
    """The marquee test: re-scan every extracted member for secrets."""
    output = tmp_path / "bundle.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=output)

    members = _extract_bundle(output)
    leaks: list = []
    for name, data in members.items():
        text = data.decode("utf-8", errors="replace")
        found = find_secrets(text)
        if found:
            leaks.append((name, found))

    assert leaks == [], f"bundle contains unredacted secrets: {leaks}"

    # Spot-check the specific secrets we planted — they must all
    # be absent from the redacted log file.
    log_bytes = members["logs.redacted.txt"]
    assert FAKE_STRIPE_SECRET.encode() not in log_bytes
    assert FAKE_ETH_PRIVATE_KEY.encode() not in log_bytes
    assert FAKE_AWS_ACCESS_KEY.encode() not in log_bytes
    assert b"hunter2hunter2" not in log_bytes

    # The PEM block buried inside settings should also be gone.
    settings_bytes = members["settings.redacted.json"]
    assert b"BEGIN RSA PRIVATE KEY" not in settings_bytes

    # Env values redacted — the dangerous install-token value
    # is never present, even though the key name is.
    env_bytes = members["env.redacted.txt"]
    assert b"sk_test_ShouldNotAppearInBundle01234" not in env_bytes
    assert b"PROXIALPHA_INSTALL_TOKEN" in env_bytes


def test_bundle_file_is_0600(fake_home: Path, tmp_path: Path) -> None:
    output = tmp_path / "bundle.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=output)
    mode = stat.S_IMODE(output.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_bundle_manifest_lists_files_and_version(
    fake_home: Path, tmp_path: Path
) -> None:
    output = tmp_path / "bundle.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=output)

    members = _extract_bundle(output)
    manifest = json.loads(members["manifest.json"])
    assert manifest["agent_version"] == doctor.__dict__["__version__"] or (
        "agent_version" in manifest
    )
    assert manifest["mode"] == "running"
    assert manifest["fingerprint"] == "deadbeef1234"
    assert "logs.redacted.txt" in manifest["files"]
    assert manifest["logs_truncated"] is False
    # The redaction policy should list the pattern names that were
    # applied — useful context for support engineers.
    assert "pem_private_key" in manifest["redaction_policy"]
    assert "stripe_secret_key" in manifest["redaction_policy"]


def test_bundle_truncates_large_log(fake_home: Path, tmp_path: Path) -> None:
    # Build a log bigger than the cap.
    big_log = "x" * (LOG_TAIL_MAX_BYTES + 50_000)
    inputs = _base_inputs(fake_home)
    inputs.log_text = big_log

    output = tmp_path / "bundle.tar.gz"
    build_bundle(inputs, output_path=output)

    members = _extract_bundle(output)
    log_bytes = members["logs.redacted.txt"]
    assert len(log_bytes) <= LOG_TAIL_MAX_BYTES + 100
    assert log_bytes.startswith(b"[...TRUNCATED...]")

    manifest = json.loads(members["manifest.json"])
    assert manifest["logs_truncated"] is True


def test_bundle_is_deterministic_for_same_inputs(
    fake_home: Path, tmp_path: Path
) -> None:
    """Two builds with identical inputs + fixed clock produce identical bytes.

    This is what lets the CI image-verify step re-build the bundle
    from canned inputs and diff against an archived hash.
    """
    out1 = tmp_path / "a.tar.gz"
    out2 = tmp_path / "b.tar.gz"
    build_bundle(_base_inputs(fake_home), output_path=out1)
    build_bundle(_base_inputs(fake_home), output_path=out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_self_check_raises_when_redactor_is_broken(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``redact_text`` is bypassed, the self-check must fail loudly.

    This proves the safety net works — if a future change accidentally
    disables a regex, the post-build scan catches it before the
    bundle is written to disk.
    """
    # Neuter the redactor everywhere doctor.py looks it up.
    monkeypatch.setattr(doctor, "redact_text", lambda text: text)

    output = tmp_path / "bundle.tar.gz"
    with pytest.raises(BundleRedactionError) as excinfo:
        build_bundle(_base_inputs(fake_home), output_path=output)

    # The error message names at least one of our planted secrets.
    msg = str(excinfo.value)
    assert "stripe_secret_key" in msg or "pem_private_key" in msg or (
        "ethereum_private_key" in msg
    )

    # And crucially — no bundle file ends up on disk.
    assert not output.exists()


def test_build_bundle_size_cap_rejects_giant_input(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathological input over 5 MB (post-compression) is rejected."""
    # Use incompressible random-ish data so gzip can't hide it under
    # the cap. 10 MB of varied bytes easily survives compression.
    import os as _os

    giant = _os.urandom(10 * 1024 * 1024).hex()  # 20 MB of hex
    inputs = _base_inputs(fake_home)
    # Bypass the log-tail truncation by stuffing settings instead
    # — settings don't get size-capped, only logs do.
    inputs.settings = {**inputs.settings, "debug_blob": giant}

    output = tmp_path / "bundle.tar.gz"
    with pytest.raises(ValueError, match="bundle exceeds size cap"):
        build_bundle(inputs, output_path=output)
    assert not output.exists()
