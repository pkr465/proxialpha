"""Background jobs for the ProxiAlpha control plane.

These modules are not part of the web-server import graph. Each one is
a standalone Python entry point designed to run under cron, a K8s
CronJob, or a Kubernetes Job. They share no global state with the
FastAPI process and must be safe to run concurrently with the web
server hitting the same database.

Current jobs
------------

* :mod:`jobs.meter_usage` — hourly metering job that reports billed
  overage usage to Stripe via the SubscriptionItem usage-records API.
  Entry point: ``python -m jobs.meter_usage``.

Design conventions
------------------

* **Sync I/O only.** All jobs use sync SQLAlchemy (``psycopg``, not
  ``asyncpg``) so they run under a plain ``python -m`` invocation
  without an asyncio event loop. The web-server code uses async
  because it needs concurrency; batch jobs have no such need.
* **JSON logs.** Each job emits one JSON line per unit of work so
  aggregators (Loki, Datadog) can parse them without custom rules.
* **Idempotent reruns.** Every job must be safe to run twice in a row.
  The metering job achieves this via deterministic Stripe idempotency
  keys and a ``reported_at`` guard column on ``usage_events``.
* **BYPASSRLS role.** Jobs connect as the ``bg_worker`` Postgres role
  which has ``BYPASSRLS`` set, so they can see rows across tenants
  without per-org ``set_config`` calls. See
  ``migrations/versions/20260411_0002_bg_worker_role.py``.
"""
