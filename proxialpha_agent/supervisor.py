"""The agent supervisor — the orchestrator that holds everything together.

The supervisor is the Phase 2 boot sequence's top-level object. It
owns the license client, the health server, the heartbeat loop,
and the mode state machine. Everything in the agent process
eventually goes through this class — it's effectively the agent's
``main()``.

Boot sequence (matches PRD §8 steps 1–13)
-----------------------------------------

1. Read :class:`~proxialpha_agent.settings.AgentSettings` from env.
2. Resolve the public key (file path or bundled dev key).
3. Build a :class:`~proxialpha_agent.license.LicenseClient`.
4. Try ``load_from_disk()``. On success, continue to step 6.
5. On ``LicenseError(reason="io")`` fall back to ``enroll()`` using
   ``settings.install_token``. Any other LicenseError → exit 1.
6. Persist the fingerprint (load_from_disk / enroll already did this).
7. Start the health server on 127.0.0.1:9877 and publish
   ``mode=BOOTING``.
8. Build a :class:`~proxialpha_agent.heartbeat.HeartbeatClient`
   with callbacks bound to this supervisor.
9. Run the heartbeat loop as a background task.
10. Wait on the shutdown event (SIGTERM handler).
11. On first successful heartbeat → transition to RUNNING.
12. On any fatal error → transition mode accordingly and exit.
13. On shutdown → transition to STOPPED, flush diary, stop health
    server, return 0.

State machine edges
-------------------

* ``BOOTING → RUNNING`` on first 2xx heartbeat.
* ``RUNNING → OFFLINE_GRACE`` on retryable heartbeat error while
  ``now < license.grace_until``.
* ``OFFLINE_GRACE → DEGRADED`` when ``now >= license.grace_until``.
* ``OFFLINE_GRACE → RUNNING`` on a subsequent 2xx.
* ``RUNNING / OFFLINE_GRACE → DEGRADED`` on 402 past-due.
* ``DEGRADED → RUNNING`` on a 2xx with a fresh token.
* ``* → REVOKED`` on 403 or 409. Terminal — triggers exit(1) after a
  5-second diary flush window.

Design notes
------------

* Mode transitions go through :meth:`_set_mode` so there's exactly
  one place to update the health snapshot and fire callbacks.
* ``exit_hook`` is injectable for tests — production passes
  ``sys.exit`` or an equivalent.
* The supervisor deliberately does NOT import the trading engine.
  Engine integration is Task 08; this task only builds the outer
  shell.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from .health import HealthServer, HealthState
from .heartbeat import (
    HeartbeatClient,
    HeartbeatError,
    HeartbeatRequest,
    HeartbeatResponse,
)
from .license import License, LicenseClient, LicenseError
from .modes import Mode
from .settings import AgentSettings

log = logging.getLogger(__name__)


#: How long we wait for diary / log flush after a fatal error
#: before actually calling the exit hook. Gives background log
#: handlers a chance to drain without losing the final message.
FATAL_FLUSH_SECONDS = 0.1

#: Current agent version string published on /health and sent to
#: the control plane on every heartbeat. Lives here so tests can
#: patch it without reaching into the distribution metadata.
AGENT_VERSION = "1.0.0"

#: Agent topology tag. Phase 2 only ships the "solo" topology —
#: Phase 3 adds "leader/follower" clustering.
AGENT_TOPOLOGY = "solo"


class Supervisor:
    """Top-level agent orchestrator. Owns license, health, heartbeat.

    Parameters
    ----------
    settings
        Parsed :class:`AgentSettings`. Holds the license and
        fingerprint paths, the control plane URL, and the health
        server bind address.
    license_client
        Pre-built :class:`LicenseClient`. Kept separate from
        ``settings`` so tests can inject a client with a
        throwaway keypair.
    health_server
        Optional pre-built :class:`HealthServer`. If omitted the
        supervisor builds one from ``settings``. Tests typically
        pass a health server bound to port 0 so the OS picks a
        free port.
    heartbeat_factory
        Callable that builds a heartbeat client given the wired
        callbacks. Tests pass a factory that returns a scripted
        stub; production leaves this None and we build a real
        :class:`HeartbeatClient`.
    now
        Injectable UTC-aware clock.
    shutdown_event
        Optional pre-constructed ``asyncio.Event``. If omitted
        the supervisor creates its own. The
        ``python -m proxialpha_agent`` entry point creates the
        event up front so it can attach a SIGTERM handler before
        ``run()`` starts.
    exit_hook
        Injectable exit function. Tests pass a recorder that
        captures the exit code instead of terminating the process.
    """

    def __init__(
        self,
        *,
        settings: AgentSettings,
        license_client: LicenseClient,
        health_server: Optional[HealthServer] = None,
        heartbeat_factory: Optional[
            Callable[..., "HeartbeatClient | _HeartbeatProtocol"]
        ] = None,
        now: Optional[Callable[[], datetime]] = None,
        shutdown_event: Optional[asyncio.Event] = None,
        exit_hook: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._settings = settings
        self._license_client = license_client
        self._health_server = health_server or HealthServer(
            host=settings.health_host,
            port=settings.health_port,
        )
        self._heartbeat_factory = heartbeat_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._shutdown_event = shutdown_event or asyncio.Event()
        self._exit_hook = exit_hook or sys.exit

        # Mutable state. Only touched from the event loop.
        self._license: Optional[License] = None
        self._mode: Mode = Mode.BOOTING
        self._started_at: datetime = self._now()
        self._mode_callbacks: List[Callable[[Mode, Mode], None]] = []
        self._heartbeat_task: Optional[asyncio.Task[Any]] = None
        self._exit_code: Optional[int] = None
        self._heartbeat_count: int = 0
        self._failure_count: int = 0

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def mode(self) -> Mode:
        """The current mode. Read-only."""
        return self._mode

    @property
    def license(self) -> Optional[License]:
        """The current verified license, or None during BOOTING."""
        return self._license

    @property
    def exit_code(self) -> Optional[int]:
        """The exit code the supervisor would pass to ``sys.exit``."""
        return self._exit_code

    def on_mode_change(
        self, callback: Callable[[Mode, Mode], None]
    ) -> None:
        """Subscribe to mode transitions.

        The callback is invoked synchronously with ``(old_mode,
        new_mode)`` on every transition, including the
        ``BOOTING → BOOTING`` no-op that :meth:`boot` emits to
        prime subscribers.
        """
        self._mode_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Boot
    # ------------------------------------------------------------------

    async def boot(self) -> None:
        """Load the license, start the health server, enter BOOTING.

        Raises :class:`LicenseError` if neither the on-disk license
        nor enrollment produced a valid license. The caller is
        expected to turn that into an ``exit(1)``.
        """
        log.info("supervisor: booting")

        # Step 1-6: load or enroll license.
        self._license = self._load_or_enroll_license()
        log.info(
            "supervisor: license ok (agent_id=%s org_id=%s exp=%s)",
            self._license.agent_id,
            self._license.org_id,
            self._license.expires_at,
        )

        # Step 7: start health server and publish initial state.
        initial_state = HealthState(
            mode=Mode.BOOTING,
            version=AGENT_VERSION,
            started_at=self._started_at,
            grace_until=self._license.grace_until,
        )
        self._health_server.set_state(initial_state)
        try:
            self._health_server.start()
        except OSError as exc:
            # Port already in use is a common dev-host error. Log
            # clearly but keep booting — a missing /health is not
            # fatal to the customer's trading engine.
            log.warning(
                "supervisor: health server failed to start (%s); "
                "continuing without it",
                exc,
            )

        # Emit a no-op transition so on_mode_change subscribers see
        # the initial mode without needing a separate bootstrap call.
        self._fire_mode_callbacks(Mode.BOOTING, Mode.BOOTING)

    def _load_or_enroll_license(self) -> License:
        """Load the license from disk, falling back to enrollment.

        This is split out so tests can call it independently of
        the full boot sequence.
        """
        try:
            return self._license_client.load_from_disk()
        except LicenseError as exc:
            if exc.reason != "io":
                # A malformed / expired / revoked license on disk
                # is fatal — we never want to silently re-enroll
                # over a tampered license.
                log.error(
                    "supervisor: on-disk license invalid (reason=%s): %s",
                    exc.reason,
                    exc,
                )
                raise

            if not self._settings.install_token:
                log.error(
                    "supervisor: no on-disk license and no install_token; "
                    "cannot bootstrap"
                )
                raise LicenseError(
                    "no on-disk license and no install_token",
                    reason="enrollment",
                ) from exc

            log.info(
                "supervisor: no license on disk; enrolling against %s",
                self._settings.control_plane_url,
            )
            return self._license_client.enroll(
                self._settings.install_token,
                control_plane_url=self._settings.control_plane_url,
            )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the heartbeat loop until shutdown or fatal error.

        Returns normally on graceful shutdown. On a fatal error
        the exit code is set via :attr:`exit_code` and the
        injected ``exit_hook`` is invoked; tests use that hook
        to verify the exit-1 path without killing pytest.
        """
        if self._license is None:
            raise RuntimeError("run() called before boot()")

        heartbeat = self._build_heartbeat_client()
        self._heartbeat_task = asyncio.create_task(
            heartbeat.run(), name="proxialpha-heartbeat"
        )

        shutdown_task = asyncio.create_task(
            self._shutdown_event.wait(), name="proxialpha-shutdown"
        )

        try:
            done, pending = await asyncio.wait(
                {self._heartbeat_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # Cancel whichever task is still running so we don't
            # leak it into the next test. asyncio raises if we
            # cancel an already-finished task, so be defensive.
            for task in (self._heartbeat_task, shutdown_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        # If the heartbeat task raised, re-raise it so tests see it.
        if self._heartbeat_task.done() and not self._heartbeat_task.cancelled():
            exc = self._heartbeat_task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                log.error("heartbeat task failed: %s", exc)

        # Graceful shutdown.
        if not self._mode.is_terminal:
            self._set_mode(Mode.STOPPED)
        self._health_server.stop()
        log.info("supervisor: run() returning (mode=%s)", self._mode.value)

    def _build_heartbeat_client(self) -> Any:
        """Build the heartbeat client with callbacks wired to self."""
        if self._heartbeat_factory is not None:
            return self._heartbeat_factory(
                token_provider=self._get_token,
                metrics_provider=self._get_metrics,
                request_factory=self._build_request,
                on_success=self._on_heartbeat_success,
                on_fatal=self._on_heartbeat_fatal,
                on_retryable_error=self._on_heartbeat_retryable,
            )
        return HeartbeatClient(
            control_plane_url=self._settings.control_plane_url,
            token_provider=self._get_token,
            metrics_provider=self._get_metrics,
            request_factory=self._build_request,
            on_success=self._on_heartbeat_success,
            on_fatal=self._on_heartbeat_fatal,
            on_retryable_error=self._on_heartbeat_retryable,
        )

    # ------------------------------------------------------------------
    # Heartbeat input plumbing
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._license is None:
            return ""
        return self._license.raw

    def _get_metrics(self) -> dict:
        """Return the current metrics snapshot.

        Phase 2 ships with a stub. Task 08 replaces this with a
        real metrics collector that reaches into the engine.
        """
        return {
            "paper_trades_last_hour": 0,
            "live_trades_last_hour": 0,
            "signals_last_hour": 0,
            "backtests_last_hour": 0,
            "errors_last_hour": 0,
        }

    def _build_request(self) -> HeartbeatRequest:
        if self._license is None:
            raise RuntimeError("_build_request called before boot()")
        now = self._now()
        return HeartbeatRequest(
            agent_id=self._license.agent_id,
            version=AGENT_VERSION,
            topology=AGENT_TOPOLOGY,
            hostname=socket.gethostname(),
            started_at=self._started_at.isoformat(),
            now=now.isoformat(),
            last_event_ts=None,
            metrics={},
        )

    # ------------------------------------------------------------------
    # Heartbeat callbacks
    # ------------------------------------------------------------------

    async def _on_heartbeat_success(self, resp: HeartbeatResponse) -> None:
        """Verify and persist the rotated license; transition to RUNNING."""
        self._heartbeat_count += 1
        try:
            new_license = self._license_client.verify(resp.license)
        except LicenseError as exc:
            log.error(
                "heartbeat success but rotated token failed verify: %s", exc
            )
            await self._fatal(
                reason="signature",
                message=f"rotated token failed verify: {exc}",
                exit_code=1,
                new_mode=Mode.REVOKED,
            )
            return

        try:
            self._license_client.persist(new_license)
        except LicenseError as exc:
            log.error(
                "heartbeat success but persist failed: %s — continuing", exc
            )

        self._license = new_license

        # Mode transitions.
        current = self._mode
        if current in (Mode.BOOTING, Mode.OFFLINE_GRACE, Mode.DEGRADED):
            self._set_mode(Mode.RUNNING)

        # Refresh health state with the new last_heartbeat_at and
        # grace_until fields.
        self._health_server.update(
            last_heartbeat_at=self._now(),
            last_heartbeat_status="ok",
            grace_until=new_license.grace_until,
        )

    async def _on_heartbeat_fatal(self, error: HeartbeatError) -> None:
        """Handle a non-retryable heartbeat error and trigger shutdown."""
        log.error(
            "heartbeat fatal: status=%s reason=%s message=%s",
            error.status_code,
            error.reason,
            error.message,
        )
        if error.status_code == 402:
            # Past-due: keep paper trading but block live. This is
            # NOT a terminal mode — a subsequent successful
            # heartbeat (e.g. after the customer pays) will move
            # back to RUNNING.
            self._set_mode(Mode.DEGRADED)
            self._health_server.update(
                last_heartbeat_status=f"past_due:{error.reason}",
            )
            return
        if error.status_code in (401, 403):
            await self._fatal(
                reason=error.reason or "revoked",
                message=error.message or f"HTTP {error.status_code}",
                exit_code=1,
                new_mode=Mode.REVOKED,
            )
            return
        if error.status_code == 409:
            await self._fatal(
                reason="fingerprint_mismatch",
                message=error.message
                or "agent_fingerprint rejected by control plane",
                exit_code=1,
                new_mode=Mode.REVOKED,
            )
            return

        # Unknown fatal status — treat as REVOKED defensively.
        await self._fatal(
            reason=error.reason or "unknown",
            message=error.message or f"HTTP {error.status_code}",
            exit_code=1,
            new_mode=Mode.REVOKED,
        )

    async def _on_heartbeat_retryable(
        self, reason: str, status: Optional[int]
    ) -> None:
        """Track retryable failures and possibly enter OFFLINE_GRACE/DEGRADED."""
        self._failure_count += 1
        self._health_server.update(
            last_heartbeat_status=f"retryable:{reason}",
            heartbeat_failures_total=self._failure_count,
        )

        current = self._mode
        if current not in (Mode.RUNNING, Mode.OFFLINE_GRACE, Mode.BOOTING):
            # DEGRADED / REVOKED / STOPPED: transitions owned by
            # other paths, no-op here.
            return

        grace_until = (
            self._license.grace_until if self._license else None
        )
        now = self._now()

        if grace_until is None:
            # No grace info; staying online on retryable errors
            # until we find out otherwise.
            return

        if now >= grace_until:
            # Grace expired → DEGRADED. We also clear the license
            # expiration check here so we stop trying to live-trade
            # on a stale license.
            if current is not Mode.DEGRADED:
                log.warning(
                    "heartbeat: grace window expired "
                    "(now=%s >= grace_until=%s); moving to DEGRADED",
                    now,
                    grace_until,
                )
                self._set_mode(Mode.DEGRADED)
            return

        if current is Mode.RUNNING or current is Mode.BOOTING:
            log.warning(
                "heartbeat: retryable error %s; entering OFFLINE_GRACE "
                "(grace_until=%s)",
                reason,
                grace_until,
            )
            self._set_mode(Mode.OFFLINE_GRACE)

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    def _set_mode(self, new_mode: Mode) -> None:
        """Transition to ``new_mode`` and notify subscribers."""
        if new_mode is self._mode:
            return
        if self._mode.is_terminal:
            # No escaping terminal modes.
            log.debug(
                "supervisor: refusing transition %s -> %s (terminal)",
                self._mode.value,
                new_mode.value,
            )
            return
        old_mode = self._mode
        self._mode = new_mode
        log.info(
            "supervisor: mode %s -> %s", old_mode.value, new_mode.value
        )
        self._health_server.update(mode=new_mode)
        self._fire_mode_callbacks(old_mode, new_mode)

    def _fire_mode_callbacks(self, old_mode: Mode, new_mode: Mode) -> None:
        for cb in list(self._mode_callbacks):
            try:
                cb(old_mode, new_mode)
            except Exception as exc:  # pragma: no cover — subscriber bug
                log.warning("mode callback raised: %s", exc)

    async def _fatal(
        self,
        *,
        reason: str,
        message: str,
        exit_code: int,
        new_mode: Mode,
    ) -> None:
        """Apply a fatal mode, signal shutdown, and call ``exit_hook``.

        Called from heartbeat fatal handlers. The exit hook runs
        AFTER the shutdown event is set so :meth:`run` has a chance
        to tear down cleanly before the process actually dies.
        """
        log.error(
            "supervisor FATAL: reason=%s message=%s -> mode=%s exit=%s",
            reason,
            message,
            new_mode.value,
            exit_code,
        )
        self._set_mode(new_mode)
        self._exit_code = exit_code
        self._health_server.update(
            last_heartbeat_status=f"fatal:{reason}",
        )
        # Give log/diary handlers a moment to flush.
        await asyncio.sleep(FATAL_FLUSH_SECONDS)
        self._shutdown_event.set()
        try:
            self._exit_hook(exit_code)
        except SystemExit:
            # Production path: sys.exit raises SystemExit, which we
            # let propagate up to the outer runner.
            raise
        except Exception as exc:  # pragma: no cover
            log.warning("exit_hook raised: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        """Signal graceful shutdown. Safe to call from a signal handler."""
        log.info("supervisor: shutdown requested")
        self._shutdown_event.set()


# ---------------------------------------------------------------------------
# Protocol stub (only used as a type hint above)
# ---------------------------------------------------------------------------


class _HeartbeatProtocol:
    """Duck-typed heartbeat interface — tests use a stub class.

    The stub only needs an ``async def run()`` method; production
    uses :class:`HeartbeatClient`.
    """

    async def run(self) -> None: ...  # noqa: D401


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def build_supervisor_from_settings(
    settings: AgentSettings,
    *,
    public_key_pem: bytes,
    now: Optional[Callable[[], datetime]] = None,
    shutdown_event: Optional[asyncio.Event] = None,
) -> Supervisor:
    """Build a production :class:`Supervisor` from parsed settings.

    Used by the ``__main__`` entry point. Tests typically build
    the pieces by hand instead of calling this.
    """
    license_client = LicenseClient(
        public_key_pem=public_key_pem,
        license_path=settings.license_path,
        fingerprint_path=settings.fingerprint_path,
        now=now,
    )
    health_server = HealthServer(
        host=settings.health_host,
        port=settings.health_port,
    )
    return Supervisor(
        settings=settings,
        license_client=license_client,
        health_server=health_server,
        now=now,
        shutdown_event=shutdown_event,
    )


# Make Path importable from this module for tests that want to
# construct a Supervisor without importing pathlib separately.
_ = Path  # noqa: F841


__all__ = [
    "AGENT_TOPOLOGY",
    "AGENT_VERSION",
    "FATAL_FLUSH_SECONDS",
    "Supervisor",
    "build_supervisor_from_settings",
]
