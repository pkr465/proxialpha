"""Control plane observability — structured logs, metrics, OTel hooks (P2-4).

This module is the control plane's answer to the gap analysis finding
"No observability": no structured JSON logging, no metrics endpoint, no
SLO surface. The first incident would have been impossible to triage.

We deliberately do NOT make ``structlog``, ``opentelemetry``, or
``prometheus_client`` *required* dependencies. The control plane already
ships a long list of runtime libraries (FastAPI + asyncpg + stripe +
yfinance + pandas + numpy + plotly + alembic + psycopg…) and adding
three more for "infra polish" would be a hard sell during the Phase 2
go-live freeze. Instead we:

* Roll our own JSON log formatter using the stdlib :mod:`logging` —
  this is enough for any log shipper (Datadog, Loki, CloudWatch, plain
  ``jq``) to parse.
* Roll our own in-process counters / histograms with a tiny Prometheus
  text serialiser — this is enough for ``/metrics`` to be scraped by
  Prometheus, the Datadog Prometheus integration, or the Grafana Agent.
* Detect ``opentelemetry`` at import time and start spans **only** when
  it's available. Production builds that want full distributed tracing
  add the OTel SDK to ``requirements.txt`` and the spans light up with
  no code change.

The whole module is wired into the FastAPI app via two functions called
from :mod:`api.server`:

* :func:`install_observability` — registers the JSON log formatter,
  the request middleware, and the ``/metrics`` route.
* :func:`record_job_run` — used by ``jobs/meter_usage.py`` to bump the
  cron-job counters from outside the request lifecycle.

The route registration is idempotent — calling ``install_observability``
twice on the same app is a no-op so reload-mode dev servers don't end
up with duplicate ``/metrics`` handlers.

JSON log format
---------------

Each record is serialised as a single line with the keys

    {
      "ts": "2026-04-11T16:30:00.123456+00:00",
      "level": "INFO",
      "logger": "api.server",
      "msg": "POST /api/backtest 200",
      "module": "server",
      "func": "_run_backtest_impl",
      "line": 421,
      "request_id": "01HV…",   # only on records emitted from a request
      "org_id": "…",            # only when the auth middleware set it
      "route": "/api/backtest", # only on per-request records
      "status_code": 200,
      "duration_ms": 142.7
    }

Extra fields beyond the stdlib ones are pulled off the LogRecord via
``record.__dict__`` so call sites can use ``log.info("...", extra=...)``
without changing call shape.

Metrics
-------

Three families are exported on ``/metrics``:

* ``http_requests_total{route, method, status}`` — counter, one per
  finished HTTP request.
* ``http_request_duration_seconds{route, method}`` — histogram with a
  small fixed bucket set tuned for an OLTP API (0.005 .. 5s).
* ``cron_job_runs_total{job, outcome}`` — counter for the cron jobs in
  ``jobs/`` (currently :mod:`jobs.meter_usage`). Outcome is one of
  ``ok``, ``error``, ``skipped_locked``.

We do not export Python process metrics (RSS, GC, fds). Those belong
to the runtime sidecar (node_exporter / cadvisor) and double-publishing
them creates dashboard ambiguity.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Context vars for request-scoped log enrichment
# ---------------------------------------------------------------------------

#: Per-request UUID, set by the middleware on entry and read by the
#: JSON formatter. ContextVar so async tasks spawned from the request
#: inherit it without having to plumb it through every call.
_request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

#: Per-request org id (string form). Set by the middleware after the
#: auth layer has resolved it onto ``request.state.org_id``. Optional —
#: an unauthed request just has an empty value.
_org_id_var: ContextVar[Optional[str]] = ContextVar("org_id", default=None)


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------


_STDLIB_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonLogFormatter(logging.Formatter):
    """Render LogRecords as one-line JSON.

    The format is intentionally flat (no nested ``meta`` object) so
    every shipper can parse it without schema config. Extra fields
    passed via ``log.info("…", extra={"foo": 1})`` end up at the top
    level alongside the stdlib fields.

    The formatter is exception-safe: if a value can't be serialised
    we coerce it to ``repr()`` rather than dropping the whole record.
    Losing visibility on a single key is much better than silently
    dropping the entire log line during an incident.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        # The base class fills in record.message; we still call its
        # ``getMessage`` so % formatting works for legacy callers.
        message = record.getMessage()
        # Use datetime directly instead of ``Formatter.formatTime``
        # because the latter wraps ``time.strftime`` which does not
        # support ``%f`` (microseconds) on every libc — caught by the
        # P2-4 smoke test where the literal ``%f`` was leaking into
        # production-shaped log output.
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }
        # Pull request-scoped enrichment from the contextvars set by
        # the middleware. We add them only when present so unrelated
        # background tasks don't end up with stray ``request_id: null``
        # noise.
        rid = _request_id_var.get()
        if rid:
            payload["request_id"] = rid
        oid = _org_id_var.get()
        if oid:
            payload["org_id"] = oid

        # Pick up any caller-supplied ``extra=`` fields.
        for key, value in record.__dict__.items():
            if key in _STDLIB_RECORD_KEYS or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value

        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc_msg"] = str(record.exc_info[1]) if record.exc_info[1] else None
            payload["stack"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str)
        except Exception as exc:  # pragma: no cover - last-resort
            return json.dumps(
                {
                    "ts": payload.get("ts"),
                    "level": "ERROR",
                    "logger": "api.observability",
                    "msg": f"json log serialisation failed: {exc}",
                    "original_msg": str(message),
                }
            )


# ---------------------------------------------------------------------------
# In-process metric registry (no prometheus_client dep)
# ---------------------------------------------------------------------------


#: Histogram buckets in seconds. Tuned for an OLTP control plane:
#: nothing slower than ~5 seconds matters at p99 because the agents
#: have their own retry budget; nothing faster than 5 ms is interesting
#: because we're network-bound to Postgres + Stripe + Clerk.
_DEFAULT_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)


class _Counter:
    """Multi-label monotonic counter.

    The label set is dynamic — we don't pre-declare permitted label
    combinations. The keyspace is bounded by route count × method
    count × distinct status codes, which is on the order of low
    hundreds for the control plane. Eviction is unnecessary.
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._lock = Lock()

    def inc(self, labels: Dict[str, str], amount: float = 1.0) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            items = list(self._values.items())
        for key, value in items:
            label_str = _format_labels(dict(key))
            lines.append(f"{self.name}{label_str} {value}")
        return lines


class _Histogram:
    """Cumulative-bucket histogram (Prometheus-compatible)."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: Tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help_text
        self.buckets = buckets
        self._buckets: Dict[Tuple[Tuple[str, str], ...], List[int]] = {}
        self._sums: Dict[Tuple[Tuple[str, str], ...], float] = {}
        self._counts: Dict[Tuple[Tuple[str, str], ...], int] = {}
        self._lock = Lock()

    def observe(self, labels: Dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            buckets = self._buckets.get(key)
            if buckets is None:
                buckets = [0] * len(self.buckets)
                self._buckets[key] = buckets
                self._sums[key] = 0.0
                self._counts[key] = 0
            for i, threshold in enumerate(self.buckets):
                if value <= threshold:
                    buckets[i] += 1
            self._sums[key] += value
            self._counts[key] += 1

    def render(self) -> List[str]:
        lines = [
            f"# HELP {self.name} {self.help}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            keys = list(self._buckets.keys())
        for key in keys:
            label_dict = dict(key)
            with self._lock:
                # ``buckets`` is already cumulative because observe()
                # increments every bucket where ``value <= threshold``,
                # so render() emits the stored counts directly. The
                # earlier version of this code re-accumulated on render
                # and produced wildly inflated values for higher
                # buckets — caught by the smoke test in P2-4.
                buckets = list(self._buckets[key])
                total_sum = self._sums[key]
                total_count = self._counts[key]
            for i, threshold in enumerate(self.buckets):
                lbl = dict(label_dict, le=_format_float(threshold))
                lines.append(f"{self.name}_bucket{_format_labels(lbl)} {buckets[i]}")
            inf_lbl = dict(label_dict, le="+Inf")
            lines.append(f"{self.name}_bucket{_format_labels(inf_lbl)} {total_count}")
            lines.append(f"{self.name}_sum{_format_labels(label_dict)} {total_sum}")
            lines.append(f"{self.name}_count{_format_labels(label_dict)} {total_count}")
        return lines


def _format_float(value: float) -> str:
    if value == int(value):
        return f"{int(value)}"
    return f"{value:g}"


def _format_labels(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    parts = []
    for k, v in sorted(labels.items()):
        # Escape per Prometheus exposition spec.
        v_escaped = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{k}="{v_escaped}"')
    return "{" + ",".join(parts) + "}"


# ---------------------------------------------------------------------------
# Module-level metric registry
# ---------------------------------------------------------------------------

http_requests_total = _Counter(
    "http_requests_total",
    "Total HTTP requests handled by the control plane.",
)
http_request_duration_seconds = _Histogram(
    "http_request_duration_seconds",
    "Latency of control plane HTTP requests in seconds.",
)
cron_job_runs_total = _Counter(
    "cron_job_runs_total",
    "Total cron job invocations grouped by outcome.",
)


def _all_metric_families() -> List[Any]:
    return [
        http_requests_total,
        http_request_duration_seconds,
        cron_job_runs_total,
    ]


def render_metrics() -> str:
    """Return the full Prometheus exposition body."""
    lines: List[str] = []
    for family in _all_metric_families():
        lines.extend(family.render())
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# OpenTelemetry hook (optional)
# ---------------------------------------------------------------------------


_otel_tracer = None


def _try_get_otel_tracer():
    """Return an OTel tracer if the SDK is installed, else ``None``.

    We import lazily and cache the result so the import cost is paid
    once per process. A missing OTel SDK is the *expected* state in
    most deployments — see the module docstring for the rationale.
    """
    global _otel_tracer
    if _otel_tracer is not None:
        return _otel_tracer
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]
    except Exception:
        return None
    _otel_tracer = trace.get_tracer("proxialpha.api")
    return _otel_tracer


# ---------------------------------------------------------------------------
# Request middleware
# ---------------------------------------------------------------------------


async def observability_middleware(request, call_next):  # type: ignore[no-untyped-def]
    """ASGI middleware that wires per-request observability.

    This middleware is intentionally last in the stack — it sees the
    final status code AFTER auth, RLS context, rate limiting, and the
    business handler have all run, so the metrics it records reflect
    the customer-visible outcome.

    Steps:

    1. Generate or honour an inbound ``X-Request-Id`` header.
    2. Stamp the request id into the contextvar so log records emitted
       inside the handler get enriched.
    3. Open an OTel span if the SDK is available.
    4. Time the call.
    5. Record the request to the http_requests_total counter and the
       duration to the histogram.
    6. Stamp the request id into the response headers so customers
       can grep their own logs against ours.

    On exceptions we still record the request — the ``status`` label
    becomes the actual exception class name as a 5xx so dashboards
    can distinguish "handler raised" from "handler returned 500".
    """
    request_id = request.headers.get("x-request-id") or _short_uuid()
    rid_token = _request_id_var.set(request_id)
    org_token = _org_id_var.set(None)

    route_label = _route_label(request)
    method = request.method
    start = time.perf_counter()
    status_code = "500"
    span_cm = None
    tracer = _try_get_otel_tracer()
    if tracer is not None:
        try:
            span_cm = tracer.start_as_current_span(
                f"HTTP {method} {route_label}",
                attributes={
                    "http.method": method,
                    "http.route": route_label,
                    "request_id": request_id,
                },
            )
            span_cm.__enter__()
        except Exception:  # pragma: no cover - defensive
            span_cm = None

    try:
        response = await call_next(request)
        status_code = str(response.status_code)
        # Promote the org id onto the contextvar AFTER the auth
        # middleware has had a chance to populate request.state.
        org = getattr(request.state, "org_id", None)
        if org is not None:
            _org_id_var.set(str(org))
        response.headers.setdefault("x-request-id", request_id)
        return response
    except Exception as exc:
        status_code = "5xx_exception"
        log.exception(
            "request handler raised: %s",
            exc,
            extra={"route": route_label, "method": method},
        )
        raise
    finally:
        duration = time.perf_counter() - start
        try:
            http_requests_total.inc(
                {"route": route_label, "method": method, "status": status_code}
            )
            http_request_duration_seconds.observe(
                {"route": route_label, "method": method},
                duration,
            )
        except Exception:  # pragma: no cover - never break a request
            pass
        log.info(
            "%s %s %s",
            method,
            route_label,
            status_code,
            extra={
                "route": route_label,
                "method": method,
                "status_code": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:  # pragma: no cover - defensive
                pass
        _request_id_var.reset(rid_token)
        _org_id_var.reset(org_token)


def _route_label(request) -> str:  # type: ignore[no-untyped-def]
    """Pick the lowest-cardinality URL label for metrics.

    FastAPI sets ``request.scope["route"].path`` to the *template*
    (e.g. ``/api/orgs/{org_id}/install-tokens``) which is the right
    cardinality for metrics — labelling on the raw URL would explode
    the time-series count. Falls back to the raw path for routes
    that haven't been matched yet (404s, middleware short-circuits).
    """
    try:
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            return route.path
    except Exception:
        pass
    return request.url.path


def _short_uuid() -> str:
    """A 26-char ULID-ish identifier — UUID4 hex without dashes."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Public installation API
# ---------------------------------------------------------------------------


_INSTALLED_APPS: "set[int]" = set()


def install_observability(app) -> None:  # type: ignore[no-untyped-def]
    """Wire the observability layer into a FastAPI application.

    Idempotent — calling twice on the same app object is a no-op so
    dev servers running under uvicorn ``--reload`` don't accumulate
    duplicate middleware. We key on ``id(app)`` because FastAPI does
    not provide a stable name for installed middleware.

    Three side effects:

    1. Reconfigure the root logger's handlers to use
       :class:`JsonLogFormatter`. Existing handlers are kept (so the
       caller can still attach a file handler) — only their formatter
       is replaced. We do NOT clear handlers and reconfigure from
       scratch because pytest captures stdout via its own handler and
       wiping that breaks the ``-s`` mode.
    2. Register :func:`observability_middleware` as a starlette
       BaseHTTPMiddleware-style middleware (the closest match for the
       request/response shape we use).
    3. Mount ``GET /metrics`` on the app.

    The metrics endpoint is intentionally NOT under ``/api`` so it
    matches the convention used by every other Prometheus scrape
    target the ops team has wired up.
    """
    if id(app) in _INSTALLED_APPS:
        return
    _INSTALLED_APPS.add(id(app))

    _install_json_logging()

    try:
        app.middleware("http")(observability_middleware)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("observability: middleware install failed: %s", exc)

    @app.get("/metrics", include_in_schema=False)
    async def _metrics_endpoint():
        from fastapi import Response

        body = render_metrics()
        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )


def _install_json_logging() -> None:
    """Replace existing log handler formatters with the JSON one.

    Honours ``LOG_FORMAT=plain`` for local debugging — JSON logs are
    miserable to read in a terminal during interactive development,
    and the env override lets a developer get the legacy single-line
    format back without editing code.
    """
    if os.environ.get("LOG_FORMAT", "json").lower() == "plain":
        return
    formatter = JsonLogFormatter()
    root = logging.getLogger()
    if not root.handlers:
        # No handlers configured yet — add a default stderr handler so
        # the JSON formatter actually has somewhere to flush.
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    # Default to INFO unless the operator has explicitly set otherwise.
    if root.level == logging.WARNING or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Cron job hook
# ---------------------------------------------------------------------------


def record_job_run(job: str, outcome: str) -> None:
    """Bump the cron-job counter from outside the request lifecycle.

    Used by ``jobs/meter_usage.py`` and any future cron job. The
    ``outcome`` label is one of ``ok``, ``error``, ``skipped_locked``
    so SLO dashboards can graph the three states separately.
    """
    try:
        cron_job_runs_total.inc({"job": job, "outcome": outcome})
    except Exception:  # pragma: no cover - never break a job
        log.warning("observability: cron counter inc failed for %s/%s", job, outcome)


__all__ = [
    "JsonLogFormatter",
    "cron_job_runs_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "install_observability",
    "observability_middleware",
    "record_job_run",
    "render_metrics",
]
