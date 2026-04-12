"""Async heartbeat client with cadence management and backoff.

The :class:`HeartbeatClient` is the agent's side of the
``POST /agent/heartbeat`` handshake implemented server-side in
:mod:`api.agent.heartbeat`. It runs forever inside the supervisor's
event loop and does three jobs:

1. **Cadence.** One heartbeat every 60 seconds in steady state. During
   the first minute of process life it runs 5 fast heartbeats on a
   10-second interval so a freshly-booted agent converges to a valid
   license / fresh entitlements snapshot quickly — this is mostly
   useful after a support rep reactivates a customer and wants the
   agent to notice within a few seconds instead of a full minute.

2. **Backoff.** On retryable errors (503, 429, connect/timeout) the
   interval doubles up to a 300-second cap. The very next success
   resets it back to 60 seconds. We do NOT back off on non-retryable
   errors — those get propagated to the supervisor immediately so it
   can make a mode transition.

3. **Error classification.** The client is the single place where
   we decide "retry internally vs. tell the supervisor". Retryable:
   503, 429, network errors. Fatal: 401, 402, 403, 409. Everything
   else is retryable-with-warning — we log loudly and keep trying.

Test-time knobs
---------------

* ``sleep`` — injectable async sleep function. Tests pass a fake
  sleep that records intervals instead of actually suspending, so
  the cadence + backoff behaviour is verifiable in milliseconds.
* ``http_client_factory`` — factory returning an
  ``httpx.AsyncClient``-compatible object. Tests pass a stub with a
  scripted queue of responses.
* ``max_iterations`` — optional cap; when set, :meth:`run` exits
  after that many heartbeats. Tests use this so
  ``await client.run()`` actually returns.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


#: Steady-state interval between heartbeats in seconds.
BASE_INTERVAL_SECONDS = 60.0

#: Fast convergence interval used during the first ``FAST_START_COUNT``
#: heartbeats. Chosen so a booting agent reaches a valid license
#: state in 10-20 seconds even if its disk-cached license was stale.
FAST_START_INTERVAL_SECONDS = 10.0

#: Number of heartbeats at the fast-start interval before we drop
#: back to the steady-state 60-second cadence.
FAST_START_COUNT = 5

#: Upper bound on the exponential backoff interval in seconds.
MAX_BACKOFF_SECONDS = 300.0

#: HTTP status codes that mean "retry internally, don't bother the
#: supervisor". 503 = control plane is down. 429 = rate limited.
RETRYABLE_STATUS_CODES = frozenset({429, 503})

#: HTTP status codes that the supervisor needs to know about right
#: away because they drive a mode transition.
FATAL_STATUS_CODES = frozenset({401, 402, 403, 409})


@dataclass
class HeartbeatResponse:
    """Parsed successful heartbeat response from the control plane.

    Mirrors :class:`api.agent.schemas.HeartbeatResponse` on the
    server side. Kept as a plain dataclass here so the agent has
    zero pydantic runtime dependency on the api package.
    """

    license: str
    config_bundle: Optional[Dict[str, Any]] = None
    rotate_token: bool = True


@dataclass
class HeartbeatError:
    """A fatal heartbeat error the supervisor needs to react to.

    ``status_code`` is the HTTP status (401/402/403/409) that
    drove the error. ``reason`` is a short tag (``"expired"``,
    ``"past_due"``, ``"canceled"``, ``"fingerprint_mismatch"``).
    ``message`` is a human-readable string for logs.
    ``grace_ends_at_iso`` is populated only for 402 responses
    where the server included a grace_ends_at field.
    """

    status_code: int
    reason: str
    message: str
    grace_ends_at_iso: Optional[str] = None


@dataclass
class HeartbeatRequest:
    """Shape of the POST body sent on every heartbeat.

    The metrics block is assembled by the supervisor's
    ``metrics_provider`` callable on each tick and embedded
    here verbatim. The control plane writes it to
    ``agents.last_metrics``.
    """

    agent_id: str
    version: str
    topology: str
    hostname: str
    started_at: str  # ISO8601 with tz
    now: str  # ISO8601 with tz
    last_event_ts: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


class HeartbeatClient:
    """Run the heartbeat loop against the control plane forever.

    The loop stops on three conditions:

    * ``asyncio.CancelledError`` — supervisor is shutting down.
    * A fatal status code arrives — we invoke the supervisor's
      ``on_fatal`` callback and return.
    * ``max_iterations`` has been reached (test-only).

    Parameters
    ----------
    control_plane_url
        Normalised base URL. The client appends ``/agent/heartbeat``.
    token_provider
        Called on every iteration to get the current license JWT.
        The supervisor updates its internal license state on each
        successful ``on_success`` call, so the next tick's provider
        call returns the freshly-rotated token automatically.
    metrics_provider
        Called on every iteration to get the current metrics
        snapshot dict. The supervisor assembles this from engine
        state (paper trades, live trades, signals, backtests,
        errors in last hour).
    request_factory
        Called on every iteration to build a :class:`HeartbeatRequest`.
        Pulling this out of the client keeps the client free of
        host/version/topology details.
    on_success
        Called with the parsed :class:`HeartbeatResponse` on each
        2xx. The supervisor uses this to rotate its cached license
        and apply any config-bundle updates.
    on_fatal
        Called with a :class:`HeartbeatError` when the control plane
        returns a FATAL_STATUS_CODES status. Loop exits immediately
        after this call.
    http_client_factory
        Factory returning an async context manager that yields an
        object with an ``async post(url, json, headers)`` method.
        Defaults to ``httpx.AsyncClient`` — tests pass a stub.
    sleep
        Async sleep callable. Defaults to ``asyncio.sleep``. Tests
        pass a recorder that fast-forwards time.
    max_iterations
        Optional cap on the number of heartbeats. None in production.
    """

    def __init__(
        self,
        *,
        control_plane_url: str,
        token_provider: Callable[[], str],
        metrics_provider: Callable[[], Dict[str, Any]],
        request_factory: Callable[[], HeartbeatRequest],
        on_success: Callable[[HeartbeatResponse], Awaitable[None] | None],
        on_fatal: Callable[[HeartbeatError], Awaitable[None] | None],
        on_retryable_error: Optional[
            Callable[[str, Optional[int]], Awaitable[None] | None]
        ] = None,
        http_client_factory: Optional[Callable[[], Any]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_iterations: Optional[int] = None,
    ) -> None:
        self._url = control_plane_url.rstrip("/") + "/agent/heartbeat"
        self._token_provider = token_provider
        self._metrics_provider = metrics_provider
        self._request_factory = request_factory
        self._on_success = on_success
        self._on_fatal = on_fatal
        self._on_retryable_error = on_retryable_error
        self._http_client_factory = http_client_factory
        self._sleep = sleep
        self._max_iterations = max_iterations

        # Mutable state. Not thread-safe but only touched from the
        # supervisor's event loop.
        self._heartbeat_count = 0
        self._current_interval = FAST_START_INTERVAL_SECONDS
        self._interval_history: List[float] = []
        # Fast-start is a one-shot convergence window. Once we've
        # either run FAST_START_COUNT heartbeats OR hit our first
        # backoff, we "graduate" to steady-state. A success that
        # resets backoff therefore returns to BASE_INTERVAL_SECONDS
        # — never back to FAST_START_INTERVAL_SECONDS.
        self._fast_start_done = False

    # ------------------------------------------------------------------
    # Public loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the heartbeat loop until cancelled or a fatal error fires."""
        log.info("heartbeat loop starting against %s", self._url)
        try:
            while True:
                if (
                    self._max_iterations is not None
                    and self._heartbeat_count >= self._max_iterations
                ):
                    log.debug(
                        "heartbeat: reached max_iterations=%s, stopping",
                        self._max_iterations,
                    )
                    return

                stop = await self._tick()
                if stop:
                    return

                interval = self._next_interval()
                self._interval_history.append(interval)
                await self._sleep(interval)
        except asyncio.CancelledError:
            log.info("heartbeat loop cancelled")
            raise

    # ------------------------------------------------------------------
    # Single tick
    # ------------------------------------------------------------------

    async def _tick(self) -> bool:
        """Perform exactly one heartbeat. Returns True to stop the loop."""
        self._heartbeat_count += 1
        try:
            resp = await self._post_once()
        except _RetryableNetworkError as exc:
            log.warning("heartbeat: network error %s; backing off", exc)
            self._apply_backoff()
            if self._on_retryable_error is not None:
                await _maybe_await(
                    self._on_retryable_error(f"network: {exc}", None)
                )
            return False
        except _FatalHTTPError as exc:
            log.error(
                "heartbeat: fatal status %s reason=%s message=%s",
                exc.status_code,
                exc.reason,
                exc.message,
            )
            error = HeartbeatError(
                status_code=exc.status_code,
                reason=exc.reason,
                message=exc.message,
                grace_ends_at_iso=exc.grace_ends_at_iso,
            )
            await _maybe_await(self._on_fatal(error))
            return True
        except _RetryableHTTPError as exc:
            log.warning(
                "heartbeat: retryable status %s; backing off", exc.status_code
            )
            self._apply_backoff()
            if self._on_retryable_error is not None:
                await _maybe_await(
                    self._on_retryable_error(
                        f"http_{exc.status_code}", exc.status_code
                    )
                )
            return False

        # Success path: reset backoff, push response to supervisor.
        self._reset_backoff()
        await _maybe_await(self._on_success(resp))
        return False

    async def _post_once(self) -> HeartbeatResponse:
        """Build one request and POST it. Raises internal exception types."""
        request = self._request_factory()
        # request_factory may have left metrics empty — let
        # metrics_provider fill them in.
        if not request.metrics:
            request.metrics = self._metrics_provider()

        token = self._token_provider()
        if not token:
            # No token means we have nothing to authenticate with;
            # treat this as a fatal 401-equivalent so the supervisor
            # can drop to REVOKED.
            raise _FatalHTTPError(
                status_code=401,
                reason="no_token",
                message="token_provider returned empty token",
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {
            "agent_id": request.agent_id,
            "version": request.version,
            "topology": request.topology,
            "hostname": request.hostname,
            "started_at": request.started_at,
            "now": request.now,
            "last_event_ts": request.last_event_ts,
            "metrics": request.metrics,
        }

        factory = self._http_client_factory or _default_async_client_factory
        try:
            async with factory() as client:
                resp = await client.post(self._url, json=body, headers=headers)
        except Exception as exc:
            raise _RetryableNetworkError(str(exc)) from exc

        status = getattr(resp, "status_code", None)
        if status is None:
            raise _RetryableNetworkError(
                "heartbeat response had no status_code attribute"
            )

        if 200 <= status < 300:
            try:
                data = resp.json()
            except Exception as exc:
                raise _RetryableNetworkError(
                    f"heartbeat 2xx but body was not JSON: {exc}"
                ) from exc
            license_token = data.get("license")
            if not license_token:
                raise _RetryableNetworkError(
                    "heartbeat 2xx but body missing 'license' field"
                )
            return HeartbeatResponse(
                license=license_token,
                config_bundle=data.get("config_bundle"),
                rotate_token=bool(data.get("rotate_token", True)),
            )

        if status in FATAL_STATUS_CODES:
            reason, message, grace_ends_at = _extract_error_fields(resp)
            raise _FatalHTTPError(
                status_code=status,
                reason=reason,
                message=message,
                grace_ends_at_iso=grace_ends_at,
            )

        if status in RETRYABLE_STATUS_CODES:
            raise _RetryableHTTPError(status_code=status)

        # Unknown non-2xx — log loudly and retry, the control plane
        # should never send these in steady state.
        log.error(
            "heartbeat: unexpected status %s from %s; treating as retryable",
            status,
            self._url,
        )
        raise _RetryableHTTPError(status_code=status)

    # ------------------------------------------------------------------
    # Cadence helpers
    # ------------------------------------------------------------------

    def _next_interval(self) -> float:
        """Return the number of seconds to sleep before the next heartbeat."""
        # If we're currently backed off, honour that interval over
        # the normal cadence — backoff wins until the next success
        # resets it.
        if self._current_interval > BASE_INTERVAL_SECONDS:
            return self._current_interval

        # Fast-start is a one-shot convergence window at boot. We
        # only apply it while the agent is still in its first
        # FAST_START_COUNT heartbeats AND has not yet hit any
        # backoff. The moment either condition fires, we lock in
        # steady-state so a recovered failure streak doesn't
        # suddenly look like a fresh boot.
        if not self._fast_start_done:
            if self._heartbeat_count >= FAST_START_COUNT:
                self._fast_start_done = True
            else:
                return FAST_START_INTERVAL_SECONDS
        return BASE_INTERVAL_SECONDS

    def _apply_backoff(self) -> None:
        """Double the interval up to the 300-second cap."""
        # Any failure graduates us out of fast-start convergence
        # mode — the recovered loop should settle into 60s cadence
        # rather than dropping back to the 10s fast-start ping.
        self._fast_start_done = True
        if self._current_interval < BASE_INTERVAL_SECONDS:
            # First failure during fast-start: jump to base and double.
            self._current_interval = BASE_INTERVAL_SECONDS
        new_interval = min(self._current_interval * 2, MAX_BACKOFF_SECONDS)
        log.debug(
            "heartbeat: backoff %.1fs -> %.1fs",
            self._current_interval,
            new_interval,
        )
        self._current_interval = new_interval

    def _reset_backoff(self) -> None:
        """Reset the interval to 60 seconds after a successful heartbeat."""
        if self._current_interval > BASE_INTERVAL_SECONDS:
            log.debug(
                "heartbeat: success; resetting interval %.1fs -> %.1fs",
                self._current_interval,
                BASE_INTERVAL_SECONDS,
            )
        self._current_interval = BASE_INTERVAL_SECONDS

    # ------------------------------------------------------------------
    # Test introspection
    # ------------------------------------------------------------------

    @property
    def heartbeat_count(self) -> int:
        """Number of heartbeats attempted so far. Test-only."""
        return self._heartbeat_count

    @property
    def interval_history(self) -> List[float]:
        """Sleep intervals recorded between heartbeats. Test-only."""
        return list(self._interval_history)

    @property
    def current_interval(self) -> float:
        """The interval that would be applied on the next sleep."""
        return self._current_interval


# ---------------------------------------------------------------------------
# Internal exception types
# ---------------------------------------------------------------------------


class _RetryableNetworkError(Exception):
    """Raised for connection errors, timeouts, and malformed 2xx bodies."""


class _RetryableHTTPError(Exception):
    """Raised for 429, 503, and unknown non-2xx responses."""

    def __init__(self, *, status_code: int) -> None:
        super().__init__(f"retryable HTTP {status_code}")
        self.status_code = status_code


class _FatalHTTPError(Exception):
    """Raised for 401/402/403/409 — propagated to the supervisor."""

    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        message: str,
        grace_ends_at_iso: Optional[str] = None,
    ) -> None:
        super().__init__(f"fatal HTTP {status_code}: {reason}")
        self.status_code = status_code
        self.reason = reason
        self.message = message
        self.grace_ends_at_iso = grace_ends_at_iso


def _extract_error_fields(resp: Any) -> tuple[str, str, Optional[str]]:
    """Pull ``reason``, ``message``, ``grace_ends_at`` out of an error body."""
    reason = "unknown"
    message = ""
    grace_ends_at: Optional[str] = None
    try:
        data = resp.json()
    except Exception:
        return reason, message, grace_ends_at
    if isinstance(data, dict):
        # FastAPI's HTTPException wraps payloads under "detail" when
        # the caller passed a dict detail.
        if "detail" in data and isinstance(data["detail"], dict):
            data = data["detail"]
        reason = str(data.get("reason") or reason)
        message = str(data.get("message") or "")
        if data.get("grace_ends_at"):
            grace_ends_at = str(data["grace_ends_at"])
    return reason, message, grace_ends_at


def _default_async_client_factory() -> Any:
    """Build a real ``httpx.AsyncClient`` with a 30-second timeout."""
    import httpx  # Deferred import so the test stub path avoids httpx.

    return httpx.AsyncClient(timeout=30.0)


async def _maybe_await(result: Any) -> None:
    """Await ``result`` if it's awaitable, otherwise no-op.

    Lets callers pass sync OR async callbacks without caring which
    — the supervisor's on_success is async but tests often pass a
    plain lambda.
    """
    if asyncio.iscoroutine(result) or (
        hasattr(result, "__await__") and callable(getattr(result, "__await__"))
    ):
        await result


__all__ = [
    "BASE_INTERVAL_SECONDS",
    "FAST_START_COUNT",
    "FAST_START_INTERVAL_SECONDS",
    "FATAL_STATUS_CODES",
    "HeartbeatClient",
    "HeartbeatError",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "MAX_BACKOFF_SECONDS",
    "RETRYABLE_STATUS_CODES",
]
