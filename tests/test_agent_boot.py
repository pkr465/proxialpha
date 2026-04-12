"""Tests for :mod:`proxialpha_agent.supervisor` and
:mod:`proxialpha_agent.health` (Task 07).

Covers the 8 boot-sequence + health-endpoint scenarios required
by the acceptance criteria:

1. Successful boot transitions BOOTING → RUNNING.
2. Retryable (503) heartbeat errors with grace_until in the future
   move the agent to OFFLINE_GRACE.
3. OFFLINE_GRACE → DEGRADED when the grace window expires.
4. HTTP 403 from the control plane drives the agent to REVOKED
   with exit code 1.
5. HTTP 409 (fingerprint mismatch) also drives exit-1.
6. HTTP 402 (past due) drops to DEGRADED — paper trading keeps
   working, live trading is blocked via
   :meth:`Mode.allows_live_trading`.
7. GET /health returns a JSON body with the current mode.
8. HealthServer refuses any non-loopback bind address.

Strategy
--------

We construct each supervisor with a pre-built
:class:`LicenseClient` (real RS256 via jwt_keys dev fallback) and
inject a ``heartbeat_factory`` that returns a scripted stub. The
stub ignores real HTTP entirely — it just invokes the supervisor's
success / fatal / retryable callbacks according to a queue we
supplied. That lets us verify mode transitions deterministically
without a running event loop waiting for real timeouts.

The supervisor's ``exit_hook`` is replaced with a recorder so we
can assert on the intended exit code without killing pytest.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

os.environ.pop("AGENT_SIGNING_KEY_PATH", None)
os.environ.pop("AGENT_SIGNING_KEY_PEM", None)
os.environ["ENV"] = "dev"

from core import jwt_keys  # noqa: E402
from proxialpha_agent.health import HealthServer, HealthState  # noqa: E402
from proxialpha_agent.heartbeat import (  # noqa: E402
    HeartbeatError,
    HeartbeatResponse,
)
from proxialpha_agent.license import LicenseClient  # noqa: E402
from proxialpha_agent.modes import Mode  # noqa: E402
from proxialpha_agent.settings import AgentSettings  # noqa: E402
from proxialpha_agent.supervisor import Supervisor  # noqa: E402


FIXED_NOW = datetime(2026, 4, 11, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scripted heartbeat stub
# ---------------------------------------------------------------------------


@dataclass
class _HeartbeatEvent:
    """A single scripted heartbeat outcome for the stub client."""

    kind: str  # "success" | "fatal" | "retryable"
    success_token: Optional[str] = None
    success_grace_until: Optional[datetime] = None
    fatal: Optional[HeartbeatError] = None
    retryable_reason: str = "network"
    retryable_status: Optional[int] = None


class FakeHeartbeatClient:
    """Replays a queue of scripted events against supervisor callbacks.

    The stub honours the same ``on_success`` / ``on_fatal`` /
    ``on_retryable_error`` contract as the real
    :class:`HeartbeatClient` — the supervisor can't tell them
    apart. ``run()`` terminates when the queue is empty so tests
    don't have to thread shutdown events through the stub.
    """

    def __init__(
        self,
        *,
        events: List[_HeartbeatEvent],
        on_success: Callable[..., Any],
        on_fatal: Callable[..., Any],
        on_retryable_error: Optional[Callable[..., Any]],
        license_signer: Callable[[datetime], str],
    ) -> None:
        self._events = list(events)
        self._on_success = on_success
        self._on_fatal = on_fatal
        self._on_retryable_error = on_retryable_error
        self._license_signer = license_signer
        self.executed: List[_HeartbeatEvent] = []

    async def run(self) -> None:
        for event in self._events:
            self.executed.append(event)
            if event.kind == "success":
                token = event.success_token or self._license_signer(
                    event.success_grace_until
                    or (FIXED_NOW + timedelta(days=7))
                )
                resp = HeartbeatResponse(
                    license=token,
                    config_bundle=None,
                    rotate_token=True,
                )
                await _maybe_await(self._on_success(resp))
            elif event.kind == "fatal":
                assert event.fatal is not None
                await _maybe_await(self._on_fatal(event.fatal))
                return  # Stop on fatal — mirrors real client.
            elif event.kind == "retryable":
                if self._on_retryable_error is not None:
                    await _maybe_await(
                        self._on_retryable_error(
                            event.retryable_reason, event.retryable_status
                        )
                    )
            else:  # pragma: no cover
                raise ValueError(f"unknown event kind: {event.kind}")
            # Yield control so mode callbacks have a chance to run.
            await asyncio.sleep(0)


async def _maybe_await(result: Any) -> None:
    if asyncio.iscoroutine(result):
        await result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_jwt_keys_cache() -> None:
    jwt_keys.reset_cache_for_tests()
    yield
    jwt_keys.reset_cache_for_tests()


@pytest.fixture
def tmp_agent_home(tmp_path: Path) -> Path:
    home = tmp_path / "proxialpha-home"
    home.mkdir()
    return home


@pytest.fixture
def settings(tmp_agent_home: Path) -> AgentSettings:
    return AgentSettings(
        control_plane_url="https://cp.example.com",
        home=tmp_agent_home,
        health_host="127.0.0.1",
        health_port=0,  # Let the OS pick a port for health tests.
        log_level="WARNING",
    )


@pytest.fixture
def license_client(tmp_agent_home: Path) -> LicenseClient:
    return LicenseClient(
        public_key_pem=jwt_keys.public_key_pem(),
        license_path=tmp_agent_home / "license",
        fingerprint_path=tmp_agent_home / "fingerprint",
        now=lambda: FIXED_NOW,
    )


def _sign_license(
    fingerprint: str,
    *,
    expires_in: timedelta = timedelta(hours=24),
    now: datetime = FIXED_NOW,
    grace_until: Optional[datetime] = None,
) -> str:
    claims = {
        "sub": "agent_alpha",
        "org_id": "org_acme",
        "agent_fingerprint": fingerprint,
        "entitlements_snapshot": {"live_trading": True},
    }
    if grace_until is not None:
        claims["grace_until"] = int(grace_until.timestamp())
    return jwt_keys.sign(claims, expires_in=expires_in, now=now)


def _persist_valid_license(
    client: LicenseClient,
    *,
    grace_until: Optional[datetime] = None,
) -> str:
    grace = grace_until or (FIXED_NOW + timedelta(days=7))
    token = _sign_license(client.fingerprint(), grace_until=grace)
    client.persist(token)
    return token


def _build_supervisor(
    *,
    settings: AgentSettings,
    license_client: LicenseClient,
    events: List[_HeartbeatEvent],
    now: datetime = FIXED_NOW,
    health_server: Optional[HealthServer] = None,
) -> Supervisor:
    exit_calls: List[int] = []

    fingerprint = license_client.fingerprint()

    def license_signer(grace_until: datetime) -> str:
        return _sign_license(fingerprint, grace_until=grace_until)

    def heartbeat_factory(
        *,
        token_provider,
        metrics_provider,
        request_factory,
        on_success,
        on_fatal,
        on_retryable_error,
    ) -> FakeHeartbeatClient:
        return FakeHeartbeatClient(
            events=events,
            on_success=on_success,
            on_fatal=on_fatal,
            on_retryable_error=on_retryable_error,
            license_signer=license_signer,
        )

    sup = Supervisor(
        settings=settings,
        license_client=license_client,
        health_server=health_server,
        heartbeat_factory=heartbeat_factory,
        now=lambda: now,
        exit_hook=exit_calls.append,
    )
    sup._test_exit_calls = exit_calls  # type: ignore[attr-defined]
    return sup


# ---------------------------------------------------------------------------
# Tests — mode machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_boot_success_reaches_running_mode(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    _persist_valid_license(license_client)
    mode_transitions: List[tuple] = []

    events = [
        _HeartbeatEvent(kind="success"),
        _HeartbeatEvent(kind="success"),
    ]
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=events,
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    sup.on_mode_change(lambda old, new: mode_transitions.append((old, new)))

    await sup.boot()
    assert sup.mode is Mode.BOOTING

    await sup.run()

    assert sup.mode in (Mode.RUNNING, Mode.STOPPED)
    # At least one BOOTING -> RUNNING transition should have fired.
    transition_pairs = [(o.value, n.value) for o, n in mode_transitions]
    assert ("booting", "running") in transition_pairs


@pytest.mark.asyncio
async def test_supervisor_503_enters_offline_grace(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    """A retryable 503 while still inside the grace window → OFFLINE_GRACE."""
    _persist_valid_license(
        license_client, grace_until=FIXED_NOW + timedelta(days=7)
    )

    mode_transitions: List[tuple] = []
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=[
            _HeartbeatEvent(
                kind="retryable",
                retryable_reason="http_503",
                retryable_status=503,
            ),
        ],
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    sup.on_mode_change(lambda old, new: mode_transitions.append((old, new)))

    await sup.boot()
    await sup.run()

    names = [(o.value, n.value) for o, n in mode_transitions]
    assert ("booting", "offline_grace") in names, (
        f"expected BOOTING -> OFFLINE_GRACE, got {names}"
    )


@pytest.mark.asyncio
async def test_supervisor_grace_expires_to_degraded(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    # Grace window is ALREADY in the past relative to the clock
    # we inject into the supervisor.
    past_grace = FIXED_NOW - timedelta(minutes=1)
    _persist_valid_license(license_client, grace_until=past_grace)

    mode_transitions: List[tuple] = []
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=[
            _HeartbeatEvent(
                kind="retryable",
                retryable_reason="http_503",
                retryable_status=503,
            ),
        ],
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    sup.on_mode_change(lambda old, new: mode_transitions.append((old, new)))
    await sup.boot()
    await sup.run()

    names = [(o.value, n.value) for o, n in mode_transitions]
    # Either direct BOOTING->DEGRADED or via OFFLINE_GRACE->DEGRADED
    # — both reach DEGRADED once the grace window has expired.
    reached_degraded = any(n == "degraded" for _, n in names)
    assert reached_degraded, (
        f"expected a transition ending in DEGRADED, got {names}"
    )


@pytest.mark.asyncio
async def test_supervisor_403_revoked_exits_1(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    _persist_valid_license(license_client)
    events = [
        _HeartbeatEvent(
            kind="fatal",
            fatal=HeartbeatError(
                status_code=403,
                reason="revoked",
                message="subscription canceled",
            ),
        ),
    ]
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=events,
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    await sup.boot()
    await sup.run()

    assert sup.mode is Mode.REVOKED
    assert sup.exit_code == 1
    # The supervisor should have invoked exit_hook with 1.
    assert 1 in sup._test_exit_calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_supervisor_409_fingerprint_mismatch_exits_1(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    _persist_valid_license(license_client)
    events = [
        _HeartbeatEvent(
            kind="fatal",
            fatal=HeartbeatError(
                status_code=409,
                reason="fingerprint_mismatch",
                message="fingerprint rejected",
            ),
        ),
    ]
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=events,
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    await sup.boot()
    await sup.run()

    assert sup.mode is Mode.REVOKED
    assert sup.exit_code == 1
    assert 1 in sup._test_exit_calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_supervisor_402_past_due_blocks_live_keeps_paper(
    settings: AgentSettings, license_client: LicenseClient
) -> None:
    _persist_valid_license(license_client)
    events = [
        _HeartbeatEvent(
            kind="fatal",
            fatal=HeartbeatError(
                status_code=402,
                reason="past_due",
                message="subscription past due",
                grace_ends_at_iso=(FIXED_NOW + timedelta(days=3)).isoformat(),
            ),
        ),
    ]
    sup = _build_supervisor(
        settings=settings,
        license_client=license_client,
        events=events,
        health_server=HealthServer(host="127.0.0.1", port=0),
    )
    await sup.boot()
    await sup.run()

    # 402 is NOT terminal — mode is DEGRADED (or STOPPED after shutdown).
    assert sup.mode in (Mode.DEGRADED, Mode.STOPPED)
    # Most importantly: live trading is blocked. Paper/backtests are
    # allowed to keep running — the engine adapters check this flag.
    assert Mode.DEGRADED.allows_live_trading is False
    assert Mode.RUNNING.allows_live_trading is True
    # And no exit was forced.
    assert sup.exit_code is None or sup.exit_code == 0


# ---------------------------------------------------------------------------
# Tests — health endpoint
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    """Fetch a URL with stdlib and return (status, body)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, body


def test_health_endpoint_returns_mode() -> None:
    """GET /health returns a JSON body containing the current mode."""
    server = HealthServer(host="127.0.0.1", port=0)
    server.set_state(
        HealthState(
            mode=Mode.RUNNING,
            version="1.0.0",
            started_at=FIXED_NOW,
            last_heartbeat_at=FIXED_NOW,
        )
    )
    server.start()
    try:
        status, body = _http_get(
            f"http://127.0.0.1:{server.port}/health"
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["mode"] == "running"
        assert payload["version"] == "1.0.0"
    finally:
        server.stop()


def test_health_endpoint_localhost_only() -> None:
    """HealthServer refuses to bind to a non-loopback address."""
    with pytest.raises(ValueError):
        HealthServer(host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        HealthServer(host="10.0.0.1", port=0)

    # Loopback addresses are accepted.
    for good in ("127.0.0.1", "::1", "localhost"):
        hs = HealthServer(host=good, port=0)
        assert hs is not None
