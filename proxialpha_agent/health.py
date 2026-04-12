"""Localhost-only HTTP server exposing ``/health`` and ``/metrics``.

This is the agent's observability surface — Prometheus scrapes
``/metrics`` and Docker healthchecks curl ``/health``. It is
deliberately minimal: a single-threaded stdlib
:class:`http.server.ThreadingHTTPServer` with two hardcoded routes
and no framework overhead. We already depend on FastAPI in the
control plane; pulling it into the customer agent would nearly
double the install footprint for very little benefit.

Security
--------

The server binds to a **loopback address only** — ``127.0.0.1``,
``::1``, or ``localhost``. Two separate gates enforce this:

1. :class:`proxialpha_agent.settings.AgentSettings` refuses to parse
   any other host at config time.
2. :meth:`HealthServer.start` re-checks the bind address right
   before calling ``server_bind`` and raises ``ValueError`` if it
   was tampered with between config parsing and startup.

This defence-in-depth matters because the agent runs on customer
hardware — we must never expose agent internals on an unexpected
interface.

State snapshot
--------------

The server holds a reference to a :class:`HealthState` snapshot
that the supervisor updates on every mode transition and every
successful heartbeat. Reads are lock-free (Python atomic object
swaps + immutable dataclasses) which is fine for a single
Prometheus scraper polling once per second.
"""
from __future__ import annotations

import http.server
import json
import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .modes import Mode

log = logging.getLogger(__name__)


#: Allowed bind addresses. The settings layer also enforces this.
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class HealthState:
    """Immutable snapshot of everything the /health endpoint exposes.

    The supervisor holds the "current" instance and swaps it
    atomically by assigning a new one. Because Python object
    reference assignment is atomic, the server's readers always
    see either the old or the new snapshot — never a torn read.
    """

    mode: Mode = Mode.BOOTING
    version: str = "unknown"
    started_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    last_heartbeat_status: Optional[str] = None
    heartbeat_failures_total: int = 0
    grace_until: Optional[datetime] = None
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_health_json(self) -> Dict[str, Any]:
        """Serialise the subset exposed on GET /health."""
        return {
            "mode": self.mode.value,
            "version": self.version,
            "started_at": _iso(self.started_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "last_heartbeat_status": self.last_heartbeat_status,
            "grace_until": _iso(self.grace_until),
        }

    def to_prometheus_text(self) -> str:
        """Render the /metrics endpoint in Prometheus exposition format.

        Kept intentionally short — we only publish the metrics
        that Phase 2 ops actually look at: agent mode, last
        heartbeat age, heartbeat failures. Engine-internal metrics
        (orders submitted, strategies active, etc.) go through the
        existing ProxiAlpha metrics path and are out of scope here.
        """
        lines = []

        # Mode as a labelled gauge — one series per mode, value 1/0.
        lines.append("# HELP proxialpha_agent_mode Current agent mode (1=active).")
        lines.append("# TYPE proxialpha_agent_mode gauge")
        for m in Mode:
            value = 1 if m is self.mode else 0
            lines.append(f'proxialpha_agent_mode{{mode="{m.value}"}} {value}')

        # Last heartbeat age in seconds. 0 if never heartbeat'd.
        lines.append("")
        lines.append(
            "# HELP proxialpha_agent_last_heartbeat_age_seconds "
            "Seconds since last successful heartbeat."
        )
        lines.append("# TYPE proxialpha_agent_last_heartbeat_age_seconds gauge")
        if self.last_heartbeat_at is not None:
            age = (
                datetime.now(timezone.utc) - self.last_heartbeat_at
            ).total_seconds()
        else:
            age = 0.0
        lines.append(f"proxialpha_agent_last_heartbeat_age_seconds {age:.3f}")

        # Heartbeat failure counter.
        lines.append("")
        lines.append(
            "# HELP proxialpha_agent_heartbeat_failures_total "
            "Cumulative heartbeat failures since process start."
        )
        lines.append("# TYPE proxialpha_agent_heartbeat_failures_total counter")
        lines.append(
            f"proxialpha_agent_heartbeat_failures_total {self.heartbeat_failures_total}"
        )

        return "\n".join(lines) + "\n"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


class _HealthRequestHandler(http.server.BaseHTTPRequestHandler):
    """Routes GET /health and GET /metrics; 404 on everything else.

    Inherits from :class:`http.server.BaseHTTPRequestHandler` so we
    get a working stdlib server with zero third-party dependencies.
    The parent :class:`HealthServer` injects itself into the handler
    class via a closure so the handler has access to the current
    :class:`HealthState`.
    """

    server_version = "ProxiAlphaAgent/1.0"

    # Overridden in HealthServer._build_handler_class to inject a
    # reference to the containing HealthServer instance.
    _owner: "HealthServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route the stdlib access log through our own logger."""
        log.debug("health http: " + format, *args)

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming convention
        if self.path == "/health":
            self._respond_health()
        elif self.path == "/metrics":
            self._respond_metrics()
        else:
            self._respond_404()

    def _respond_health(self) -> None:
        snapshot = self._owner.snapshot()
        body = json.dumps(snapshot.to_health_json()).encode("utf-8")
        status = 200 if snapshot.mode is not Mode.REVOKED else 503
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_metrics(self) -> None:
        snapshot = self._owner.snapshot()
        body = snapshot.to_prometheus_text().encode("utf-8")
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/plain; version=0.0.4; charset=utf-8"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_404(self) -> None:
        body = b'{"error":"not_found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HealthServer:
    """Threaded HTTP server that serves /health and /metrics.

    The server runs on a daemon thread so the asyncio event loop
    stays untouched. Stop it by calling :meth:`stop`, which
    shuts down the server and joins the thread.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        initial_state: Optional[HealthState] = None,
    ) -> None:
        if host not in LOOPBACK_ADDRESSES:
            raise ValueError(
                f"health_host must be a loopback address (got {host!r}); "
                f"allowed: {sorted(LOOPBACK_ADDRESSES)}"
            )
        self._host = host
        self._port = port
        self._state: HealthState = initial_state or HealthState()
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def snapshot(self) -> HealthState:
        """Return the current snapshot. Safe to call from any thread."""
        return self._state

    def update(self, **changes: Any) -> HealthState:
        """Patch the current snapshot and return the new one.

        Uses :func:`dataclasses.replace` so we always swap in an
        immutable new instance rather than mutating in place. The
        lock only serialises the read-modify-write, not the reads.
        """
        with self._lock:
            new_state = replace(self._state, **changes)
            self._state = new_state
            return new_state

    def set_state(self, new_state: HealthState) -> None:
        """Replace the snapshot wholesale. Used by the supervisor on boot."""
        with self._lock:
            self._state = new_state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the socket and spin up the daemon thread."""
        if self._server is not None:
            raise RuntimeError("HealthServer already started")
        if self._host not in LOOPBACK_ADDRESSES:
            # Defence in depth — if somebody mutated self._host
            # between __init__ and start(), refuse to serve.
            raise ValueError(
                f"refusing to bind non-loopback host {self._host!r}"
            )

        handler_class = self._build_handler_class()
        self._server = http.server.ThreadingHTTPServer(
            (self._host, self._port), handler_class
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="proxialpha-health",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "health server listening on http://%s:%s (loopback only)",
            self._host,
            self._port,
        )

    def stop(self) -> None:
        """Shut down the server and join its thread."""
        if self._server is None:
            return
        log.info("health server stopping")
        try:
            self._server.shutdown()
        except Exception as exc:  # pragma: no cover
            log.warning("health server shutdown error: %s", exc)
        try:
            self._server.server_close()
        except Exception as exc:  # pragma: no cover
            log.warning("health server close error: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_handler_class(self) -> type:
        """Subclass the handler so we can inject a reference to ``self``.

        The stdlib ``BaseHTTPRequestHandler`` takes no constructor
        args we control — instantiation happens inside the server
        for each request. A closure-bound subclass is the cleanest
        way to hand the handler a reference back to the owning
        :class:`HealthServer`.
        """
        owner = self

        class _BoundHandler(_HealthRequestHandler):
            _owner = owner  # type: ignore[assignment]

        return _BoundHandler

    # Context manager sugar — tests use `with HealthServer(...) as hs:`.
    def __enter__(self) -> "HealthServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        """Return the actual bound port — useful when port=0 was passed."""
        if self._server is None:
            return self._port
        return self._server.server_address[1]  # type: ignore[return-value]


__all__ = ["HealthServer", "HealthState", "LOOPBACK_ADDRESSES"]
