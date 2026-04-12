"""Tests for :mod:`proxialpha_agent.heartbeat` (Task 07).

Covers the three cadence / backoff behaviours required by the
acceptance criteria:

1. Steady-state 60-second cadence after the fast-start window.
2. Exponential backoff on 503 responses, with the cap enforced.
3. Reset to 60 seconds after the first success following a
   backed-off failure streak.

No real HTTP. We inject a ``FakeAsyncClient`` that returns a
scripted queue of responses, and a ``FakeSleep`` that records
requested sleep intervals without actually suspending. That
means the whole loop runs in milliseconds and cadence is
verifiable by inspecting the list of recorded sleeps.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from proxialpha_agent.heartbeat import (  # noqa: E402
    BASE_INTERVAL_SECONDS,
    FAST_START_COUNT,
    FAST_START_INTERVAL_SECONDS,
    MAX_BACKOFF_SECONDS,
    HeartbeatClient,
    HeartbeatError,
    HeartbeatRequest,
    HeartbeatResponse,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    """Minimal stand-in for an ``httpx.Response``."""

    status_code: int
    body: Dict[str, Any]
    text: str = ""

    def json(self) -> Dict[str, Any]:
        return self.body


class FakeAsyncClient:
    """Async context manager that returns a scripted queue of responses."""

    def __init__(self, responses: List[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: Dict[str, Any],
        headers: Dict[str, str],
    ) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            # Default to a generic success so the loop keeps running
            # without blowing up on an empty queue in case a test
            # under-scripts.
            return FakeResponse(
                status_code=200, body={"license": "fake-token"}
            )
        return self._responses.pop(0)


class FakeSleep:
    """Awaitable no-op that records every interval request."""

    def __init__(self) -> None:
        self.intervals: List[float] = []

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)


def _make_request_factory() -> HeartbeatRequest:
    return HeartbeatRequest(
        agent_id="agent_test",
        version="1.0.0",
        topology="solo",
        hostname="test-host",
        started_at="2026-04-11T12:00:00+00:00",
        now="2026-04-11T12:00:00+00:00",
        last_event_ts=None,
        metrics={},
    )


def _success(license_token: str = "next-token") -> FakeResponse:
    return FakeResponse(
        status_code=200,
        body={"license": license_token, "rotate_token": True},
    )


def _retryable(status: int) -> FakeResponse:
    return FakeResponse(status_code=status, body={"error": "try_later"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_client_60s_cadence() -> None:
    """After FAST_START_COUNT heartbeats, cadence settles at 60s."""
    # Queue up 8 successes — enough to clear the fast-start window
    # and exercise the steady-state interval.
    responses = [_success() for _ in range(8)]
    fake_client = FakeAsyncClient(responses)
    fake_sleep = FakeSleep()
    successes: List[HeartbeatResponse] = []

    def on_success(resp: HeartbeatResponse) -> None:
        successes.append(resp)

    client = HeartbeatClient(
        control_plane_url="https://cp.example.com",
        token_provider=lambda: "bearer-token",
        metrics_provider=lambda: {"k": 1},
        request_factory=_make_request_factory,
        on_success=on_success,
        on_fatal=lambda e: None,
        http_client_factory=lambda: fake_client,
        sleep=fake_sleep,
        max_iterations=8,
    )

    await client.run()

    # Fast-start window: first FAST_START_COUNT sleeps should be 10s.
    assert len(fake_sleep.intervals) == 8
    for i in range(FAST_START_COUNT - 1):
        assert fake_sleep.intervals[i] == FAST_START_INTERVAL_SECONDS, (
            f"interval[{i}] expected {FAST_START_INTERVAL_SECONDS}, "
            f"got {fake_sleep.intervals[i]}"
        )

    # After the fast-start window, cadence is 60s.
    for i in range(FAST_START_COUNT, 8):
        assert fake_sleep.intervals[i] == BASE_INTERVAL_SECONDS, (
            f"interval[{i}] expected {BASE_INTERVAL_SECONDS}, "
            f"got {fake_sleep.intervals[i]}"
        )

    # 8 heartbeats all succeeded -> 8 on_success calls.
    assert len(successes) == 8
    assert all(r.license == "next-token" for r in successes)


@pytest.mark.asyncio
async def test_heartbeat_client_backoff_on_503() -> None:
    """On a streak of 503s the interval doubles up to the cap."""
    # 6 consecutive 503s.
    responses = [_retryable(503) for _ in range(6)]
    fake_client = FakeAsyncClient(responses)
    fake_sleep = FakeSleep()

    client = HeartbeatClient(
        control_plane_url="https://cp.example.com",
        token_provider=lambda: "bearer-token",
        metrics_provider=lambda: {},
        request_factory=_make_request_factory,
        on_success=lambda r: None,
        on_fatal=lambda e: None,
        http_client_factory=lambda: fake_client,
        sleep=fake_sleep,
        max_iterations=6,
    )

    await client.run()

    # After 6 failures the intervals should be monotonically
    # non-decreasing and capped at MAX_BACKOFF_SECONDS.
    assert len(fake_sleep.intervals) == 6
    for i in range(1, len(fake_sleep.intervals)):
        assert fake_sleep.intervals[i] >= fake_sleep.intervals[i - 1], (
            f"backoff should not shrink: {fake_sleep.intervals}"
        )
    assert max(fake_sleep.intervals) <= MAX_BACKOFF_SECONDS

    # At least one of the intervals should be > base interval —
    # i.e., we actually backed off rather than staying at 10s/60s.
    assert any(iv > BASE_INTERVAL_SECONDS for iv in fake_sleep.intervals), (
        f"expected at least one interval > {BASE_INTERVAL_SECONDS}, "
        f"got {fake_sleep.intervals}"
    )

    # Client's internal current_interval should also be > base.
    assert client.current_interval > BASE_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_heartbeat_client_resets_to_60s_on_success_after_backoff() -> None:
    """A single success after a backoff streak resets interval to 60s."""
    # Queue: three 503s, then one success, then one more tick.
    responses = [
        _retryable(503),
        _retryable(503),
        _retryable(503),
        _success(),
        _success(),
    ]
    fake_client = FakeAsyncClient(responses)
    fake_sleep = FakeSleep()

    client = HeartbeatClient(
        control_plane_url="https://cp.example.com",
        token_provider=lambda: "bearer-token",
        metrics_provider=lambda: {},
        request_factory=_make_request_factory,
        on_success=lambda r: None,
        on_fatal=lambda e: None,
        http_client_factory=lambda: fake_client,
        sleep=fake_sleep,
        max_iterations=5,
    )

    await client.run()

    # The interval after the first success (index 3) should be back
    # to the steady-state base interval, even though index 2 (the
    # third failure) was in the backed-off range.
    assert fake_sleep.intervals[2] > BASE_INTERVAL_SECONDS, (
        f"third failure should be backed off, got "
        f"{fake_sleep.intervals[2]}"
    )
    assert fake_sleep.intervals[3] == BASE_INTERVAL_SECONDS, (
        f"first sleep after success should be {BASE_INTERVAL_SECONDS}, "
        f"got {fake_sleep.intervals[3]}"
    )
    assert fake_sleep.intervals[4] == BASE_INTERVAL_SECONDS, (
        f"steady state after recovery should be {BASE_INTERVAL_SECONDS}, "
        f"got {fake_sleep.intervals[4]}"
    )

    # And the client's internal state confirms the reset.
    assert client.current_interval == BASE_INTERVAL_SECONDS
