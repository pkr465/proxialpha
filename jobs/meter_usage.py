"""Hourly metering job — reports billed overage to Stripe.

This is Task 05. It is the single path by which overage usage that the
entitlement decorator (Task 04) wrote into ``usage_events`` turns into
money on the customer's Stripe invoice.

Concurrency model
-----------------

This is a **batch, single-writer** job. It is expected to run under
cron (or a K8s CronJob) once an hour. It reads unreported billed
events, buckets them by (org, feature, hour), and calls
``stripe.SubscriptionItem.create_usage_record`` once per bucket.

Correctness does NOT depend on holding a global lock — every Stripe
call is keyed with a deterministic idempotency_key derived from
``(org_id, feature, hour_epoch)``, every DB mutation is guarded by
``WHERE reported_at IS NULL``, and Stripe's ``action="set"`` semantics
collapse duplicate writes. A duplicate run is therefore safe.

But the job ALSO acquires a Postgres ``pg_try_advisory_lock`` (key
``ADVISORY_LOCK_KEY``) before doing any work. This is belt-and-
suspenders for cron drift, manual re-runs, and the case where a slow
DB transaction in run N is still committing when run N+1 starts. The
lock is session-scoped, so if a worker crashes the next run picks up
where it left off without manual intervention. P2-6 in the gap
analysis pinned this as a must-have before the first paying customer.

If the lock is already held, the job logs ``msg=skipped_locked`` and
returns immediately with ``stats["skipped_locked"]=1`` — no error,
no cron alarm.

Safety window
-------------

We ignore any event whose ``occurred_at`` is within the last 5 minutes.
Why: the entitlement decorator's atomic consume runs in one
transaction with the ``usage_events`` insert, but FastAPI's response
might still be mid-flight when cron starts. Waiting 5 minutes gives
every in-flight request time to commit its event row before we meter
the bucket. Without this window, a bucket could be reported *before*
a late-arriving event from the same hour landed, leading to an
under-report. (``action="set"`` would let a subsequent run correct it,
but the extra noise isn't worth the few-minute delay.)

Dry-run mode
------------

``--dry-run`` reads events and logs what would be reported, but never
calls Stripe and never mutates the database. Use this during
deployments and when validating new code against a prod snapshot.

RLS and the ``bg_worker`` role
------------------------------

The job connects to Postgres as the ``bg_worker`` role, which has
``BYPASSRLS`` set (see migration ``0002_bg_worker_role``). Without
BYPASSRLS the job would see zero rows because no ``app.current_org_id``
GUC is set. We intentionally do NOT call ``set_config`` here — we
operate across all tenants.

Entry point
-----------

    python -m jobs.meter_usage           # normal mode
    python -m jobs.meter_usage --dry-run # print-only mode

Logs are emitted as one JSON object per line to stdout, with fields:
``ts``, ``level``, ``job``, ``org_id``, ``feature``, ``bucket``,
``qty``, ``stripe_id``, ``error``. Loki / Datadog can ingest these
with the default JSON parser.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine, Row

# Local import — the wrapper exposes a single callable plus an exception.
from core.stripe_client import StripeReportError, report_usage

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How far back a usage event must be before we attempt to report it.
#: 5 minutes gives in-flight HTTP requests time to commit their event
#: rows before the hourly bucket closes. See module docstring.
SAFETY_WINDOW = timedelta(minutes=5)

#: The name every log line is tagged with, for aggregator filtering.
JOB_NAME = "meter_usage"

#: Postgres advisory-lock key. Computed at import time so the same
#: integer is used by every worker. We pick a fixed bigint rather than
#: ``hashtext()`` so the value is reproducible across Postgres major
#: versions (``hashtext`` is documented as unstable across upgrades).
#: Any positive bigint works — the value is opaque to Postgres and
#: the only contract is that no other job in the cluster picks the
#: same one. Reserved range: 4242_0000–4242_9999 for this codebase.
ADVISORY_LOCK_KEY = 4242_0001


# ---------------------------------------------------------------------------
# JSON logging
# ---------------------------------------------------------------------------


def _log_json(
    level: str,
    msg: str,
    *,
    stream: Any = None,
    **fields: Any,
) -> None:
    """Emit one JSON line.

    We deliberately don't use the ``logging`` module's JSON formatters
    — those pull in pkg_resources and add startup latency. A plain
    ``json.dumps`` keeps the job's import graph tiny.

    ``level`` is upper-cased ("INFO" / "WARN" / "ERROR"). ``msg`` is a
    short machine-readable tag (``"reported"``, ``"skipped"``,
    ``"stripe_error"``) that log consumers can filter on.
    """
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "job": JOB_NAME,
        "msg": msg,
    }
    # Serialise datetimes and UUIDs cleanly.
    for key, value in fields.items():
        if isinstance(value, datetime):
            record[key] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            record[key] = str(value)
        else:
            record[key] = value
    out = stream if stream is not None else sys.stdout
    out.write(json.dumps(record, default=str) + "\n")
    out.flush()


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _coerce_dt(value: Any) -> datetime:
    """Normalise an ``occurred_at`` value to a tz-aware UTC datetime.

    Postgres (via psycopg) returns a tz-aware ``datetime``. SQLite
    stores it as ``TEXT`` and may return either an ISO string with
    offset or a space-separated naive string depending on which
    default was used on INSERT. We handle both.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        raise TypeError(
            f"_coerce_dt expected datetime or str, got {type(value).__name__}"
        )
    # fromisoformat handles "2026-04-11T10:30:00+00:00" and
    # "2026-04-11 10:30:00" (from 3.11+) and most common forms.
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Final fallback: strip microseconds, assume naive UTC.
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _hour_floor(dt: datetime) -> datetime:
    """Return the datetime rounded DOWN to the start of its hour (UTC)."""
    return dt.replace(minute=0, second=0, microsecond=0)


def _bucket_epoch(dt: datetime) -> int:
    """Unix timestamp (seconds) at the start of the hour bucket."""
    return int(_hour_floor(dt).timestamp())


def _build_idempotency_key(org_id: str, feature: str, bucket_epoch: int) -> str:
    """Deterministic Stripe idempotency key for a (org, feature, hour) bucket.

    Load-bearing: if this format ever changes, replays of events that
    were written under the old format will get NEW Stripe records
    instead of hitting the existing one, which would be a double-bill.
    Do not change this without a migration plan.
    """
    return f"usage_{org_id}_{feature}_{bucket_epoch}"


# ---------------------------------------------------------------------------
# DB access — plain sync SQLAlchemy
# ---------------------------------------------------------------------------


_FETCH_UNREPORTED_SQL = text(
    """
    SELECT id, org_id, feature, quantity, occurred_at
    FROM usage_events
    WHERE billed = 1
      AND reported_at IS NULL
      AND occurred_at < :cutoff
    ORDER BY org_id, feature, occurred_at
    """
)


_FETCH_METERED_ITEMS_SQL = text(
    """
    SELECT org_id, metered_item_ids
    FROM subscriptions
    WHERE org_id IN :org_ids
    ORDER BY updated_at DESC
    """
).bindparams(bindparam("org_ids", expanding=True))


_MARK_REPORTED_SQL = text(
    """
    UPDATE usage_events
    SET reported_at = :now,
        stripe_usage_record_id = :stripe_id
    WHERE id IN :ids
      AND reported_at IS NULL
    """
).bindparams(bindparam("ids", expanding=True))


def _fetch_unreported(conn: Connection, cutoff: datetime) -> List[Row]:
    """Return all unreported billed events older than ``cutoff``.

    The cutoff is passed as an ISO-8601 string so both the Postgres
    psycopg driver and SQLite compare it correctly against
    ``occurred_at``. Postgres psycopg accepts the string as a
    ``timestamptz``; SQLite uses lex comparison which is correct for
    this format.
    """
    result = conn.execute(
        _FETCH_UNREPORTED_SQL, {"cutoff": cutoff.isoformat()}
    )
    return list(result.fetchall())


def _fetch_metered_items(
    conn: Connection, org_ids: List[str]
) -> Dict[str, Dict[str, str]]:
    """Return ``{org_id: {feature_name: stripe_subscription_item_id}}``.

    We take the MOST RECENT subscription per org (ordered by
    ``updated_at DESC``) as authoritative. ``metered_item_ids`` is a
    JSON object written by the webhook handler — in Postgres it's
    ``jsonb``, in SQLite tests it's ``TEXT``, so we ``json.loads`` on
    every read for dialect portability.
    """
    if not org_ids:
        return {}
    result = conn.execute(_FETCH_METERED_ITEMS_SQL, {"org_ids": org_ids})
    out: Dict[str, Dict[str, str]] = {}
    for row in result.fetchall():
        org_id = str(row[0])
        if org_id in out:
            # Already captured the newest (ORDER BY updated_at DESC).
            continue
        raw = row[1]
        if raw is None:
            parsed: Dict[str, str] = {}
        elif isinstance(raw, (dict, Mapping)):
            parsed = {str(k): str(v) for k, v in raw.items()}
        else:
            try:
                parsed = {str(k): str(v) for k, v in json.loads(raw).items()}
            except (TypeError, ValueError):
                parsed = {}
        out[org_id] = parsed
    return out


def _mark_reported(
    conn: Connection,
    *,
    row_ids: List[str],
    stripe_id: Optional[str],
    now: datetime,
) -> None:
    """Write ``reported_at`` + ``stripe_usage_record_id`` for a bucket.

    We include ``reported_at IS NULL`` in the WHERE so a second job
    run (or a concurrent worker) can't double-apply. ``stripe_id`` is
    allowed to be NULL when the caller is in dry-run mode; in the
    normal path Stripe always returns an id.
    """
    if not row_ids:
        return
    conn.execute(
        _MARK_REPORTED_SQL,
        {"ids": row_ids, "stripe_id": stripe_id, "now": now.isoformat()},
    )


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


#: A single aggregated bucket's payload: which event rows it covers
#: and the summed quantity across them.
BucketKey = Tuple[str, str, datetime]  # (org_id, feature, hour_floor_dt)


def _group_into_buckets(
    rows: List[Row],
) -> Dict[BucketKey, Dict[str, Any]]:
    """Reduce raw event rows to per-hour buckets.

    We do the bucketing in Python (not SQL) because ``date_trunc`` is
    Postgres-specific and ``strftime`` is SQLite-specific. Pushing it
    into Python keeps the SQL identical across both dialects and
    means tests don't need a dialect-aware fixture.
    """
    buckets: Dict[BucketKey, Dict[str, Any]] = defaultdict(
        lambda: {"ids": [], "qty": 0}
    )
    for row in rows:
        row_id = str(row[0])
        org_id = str(row[1])
        feature = str(row[2])
        qty = int(row[3])
        occurred_at = _coerce_dt(row[4])
        bucket_dt = _hour_floor(occurred_at)
        key: BucketKey = (org_id, feature, bucket_dt)
        buckets[key]["ids"].append(row_id)
        buckets[key]["qty"] += qty
    return buckets


# ---------------------------------------------------------------------------
# Main job entry point
# ---------------------------------------------------------------------------


#: Type alias for a Stripe reporter callable. Takes the same
#: positional/keyword arguments as :func:`core.stripe_client.report_usage`
#: and returns the Stripe response dict (must contain ``id``).
StripeReporter = Callable[..., Dict[str, Any]]


def _try_acquire_advisory_lock(conn: Connection) -> bool:
    """Acquire the meter_usage advisory lock; return False if held.

    Postgres-only: ``pg_try_advisory_lock(bigint)`` returns immediately
    with True if the lock was free or False if another session holds
    it. The lock is **session-scoped** — it auto-releases when the
    connection closes, so a crashed worker doesn't wedge the next run.

    SQLite path: this function checks the dialect name and short-
    circuits to True under SQLite (test path), because the test
    fixtures don't run two workers in parallel and SQLite has no
    advisory-lock primitive.

    Returns True iff the caller now owns the lock (and MUST eventually
    release it via ``_release_advisory_lock``, OR close the connection).
    """
    dialect = conn.dialect.name if conn.dialect is not None else ""
    if dialect != "postgresql":
        return True
    result = conn.execute(
        text("SELECT pg_try_advisory_lock(:key)"),
        {"key": ADVISORY_LOCK_KEY},
    )
    row = result.fetchone()
    return bool(row[0]) if row is not None else False


def _release_advisory_lock(conn: Connection) -> None:
    """Release the meter_usage advisory lock if we hold it.

    Best-effort: we swallow any error so a release failure (e.g. the
    connection died) does not mask the original job result. The lock
    auto-releases on connection close anyway.
    """
    dialect = conn.dialect.name if conn.dialect is not None else ""
    if dialect != "postgresql":
        return
    try:
        conn.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": ADVISORY_LOCK_KEY},
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log_json("WARN", "advisory_unlock_failed", error=str(exc))


def run(
    *,
    engine: Engine,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    stripe_reporter: Optional[StripeReporter] = None,
) -> Dict[str, int]:
    """Execute one pass of the hourly metering job.

    Parameters
    ----------
    engine
        A sync SQLAlchemy ``Engine``. In production this points at
        Postgres via ``postgresql+psycopg://``; in tests it's a
        SQLite in-memory engine.
    dry_run
        If True, no Stripe calls and no DB writes are performed.
        Every bucket is still logged (as ``level=INFO msg=dry_run``)
        so an operator can eyeball what would be billed.
    now
        Override for the "current time" used to compute the safety
        cutoff. Tests pass a fixed datetime; production omits this
        and gets ``datetime.now(UTC)``.
    stripe_reporter
        Override for the Stripe-calling function. Tests pass a fake
        that records calls to an in-memory list. In production this
        defaults to :func:`core.stripe_client.report_usage`.

    Returns
    -------
    dict
        ``{"processed": int, "skipped": int, "errored": int,
          "dry_run": bool, "buckets": int}``

        * ``processed`` — buckets successfully reported (and DB updated).
        * ``skipped`` — buckets that had no matching subscription item
          (e.g. the sub was canceled before metering ran). These rows
          stay unreported and an operator must investigate.
        * ``errored`` — buckets that hit a Stripe error. Rows stay
          unreported so the next run will retry with the same
          idempotency key.
        * ``dry_run`` — echoes the input flag.
        * ``buckets`` — total number of buckets seen.
    """
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    cutoff = effective_now - SAFETY_WINDOW
    reporter: StripeReporter = stripe_reporter or report_usage

    stats = {
        "processed": 0,
        "skipped": 0,
        "errored": 0,
        "dry_run": bool(dry_run),
        "buckets": 0,
        "skipped_locked": 0,
    }

    # P2-6: hold the advisory lock for the entire job. We open a single
    # connection up front, try to take the lock, and bail with
    # ``skipped_locked=1`` if another worker is mid-run. Per the module
    # docstring the job is correct without this lock — Stripe
    # idempotency keys make duplicate runs safe — but the lock keeps
    # the logs quiet during cron drift and prevents two workers from
    # racing on the same ``UPDATE usage_events SET reported_at`` and
    # paying double network round-trip cost.
    # P2-4: surface this run's outcome on the cron_job_runs_total
    # counter so the SLO dashboard can graph cron success vs. lock
    # contention vs. real failures. The import is local + best-effort
    # so the cron job stays runnable in a slim test checkout that
    # hasn't built the API package.
    try:
        from api.observability import record_job_run as _record_job_run
    except Exception:  # pragma: no cover - optional dep fallback
        def _record_job_run(job: str, outcome: str) -> None:  # type: ignore[no-redef]
            return None

    lock_conn = engine.connect()
    try:
        if not _try_acquire_advisory_lock(lock_conn):
            _log_json(
                "INFO",
                "skipped_locked",
                reason="another worker holds the advisory lock",
                key=ADVISORY_LOCK_KEY,
            )
            stats["skipped_locked"] = 1
            _record_job_run("meter_usage", "skipped_locked")
            return stats

        # 1) Fetch unreported events in one read-only query. Reuse the
        #    lock-holding connection so a long-running fetch can't be
        #    interrupted by a competing worker that grabs the lock
        #    between the connect() and the SELECT.
        rows = _fetch_unreported(lock_conn, cutoff)

        if not rows:
            _log_json("INFO", "no_pending_events", cutoff=cutoff.isoformat())
            _record_job_run("meter_usage", "ok")
            return stats

        # 2) Group to per-hour buckets in Python (dialect-independent).
        buckets = _group_into_buckets(rows)
        stats["buckets"] = len(buckets)

        # 3) Resolve each org's metered subscription-item IDs once.
        org_ids = sorted({k[0] for k in buckets})
        sub_map = _fetch_metered_items(lock_conn, org_ids)

        # 4) Process each bucket.
        #
        # Buckets are processed in sorted order so the log output is
        # deterministic and a failure mid-run doesn't leave "earlier"
        # unprocessed buckets interleaved with later processed ones.
        for key in sorted(buckets.keys()):
            org_id, feature, bucket_dt = key
            info = buckets[key]
            qty = int(info["qty"])
            row_ids = list(info["ids"])
            epoch = int(bucket_dt.timestamp())
            idem = _build_idempotency_key(org_id, feature, epoch)

            # a) Resolve the subscription item ID for this feature.
            sub_items = sub_map.get(org_id, {})
            si_id = sub_items.get(feature)
            if not si_id:
                _log_json(
                    "WARN",
                    "no_sub_item",
                    org_id=org_id,
                    feature=feature,
                    bucket=epoch,
                    qty=qty,
                )
                stats["skipped"] += 1
                continue

            # b) Dry-run short-circuit.
            if dry_run:
                _log_json(
                    "INFO",
                    "dry_run",
                    org_id=org_id,
                    feature=feature,
                    bucket=epoch,
                    qty=qty,
                    idempotency_key=idem,
                    subscription_item=si_id,
                )
                stats["processed"] += 1
                continue

            # c) Real Stripe call.
            try:
                record = reporter(
                    si_id,
                    qty,
                    idempotency_key=idem,
                    timestamp=epoch,
                )
            except StripeReportError as exc:
                _log_json(
                    "ERROR",
                    "stripe_error",
                    org_id=org_id,
                    feature=feature,
                    bucket=epoch,
                    qty=qty,
                    error=str(exc),
                )
                stats["errored"] += 1
                continue

            stripe_id = None
            if isinstance(record, dict):
                stripe_id = record.get("id")

            # d) Persist the reported_at + stripe_usage_record_id. We use a
            #    fresh transaction per bucket so a later bucket's failure
            #    never rolls back an earlier bucket's successful Stripe call.
            #    This uses a SEPARATE engine connection (not lock_conn) so
            #    the per-bucket commit can't accidentally release the
            #    advisory lock the outer connection is holding.
            with engine.begin() as write_conn:
                _mark_reported(
                    write_conn,
                    row_ids=row_ids,
                    stripe_id=stripe_id,
                    now=effective_now,
                )

            _log_json(
                "INFO",
                "reported",
                org_id=org_id,
                feature=feature,
                bucket=epoch,
                qty=qty,
                stripe_id=stripe_id,
                idempotency_key=idem,
            )
            stats["processed"] += 1

        # Outcome label is "error" if any bucket failed mid-loop, even
        # if other buckets succeeded — the SLO surface is "did this run
        # complete cleanly", and a partial run still needs operator
        # attention. The successful per-bucket counters live on the
        # individual ``processed`` / ``errored`` fields in stats.
        outcome = "error" if stats["errored"] > 0 else "ok"
        _record_job_run("meter_usage", outcome)
        return stats
    except Exception:
        # An unexpected exception (DB connection drop, asyncpg fault,
        # programmer error in the bucketing code) is the worst class
        # of failure — record it explicitly before re-raising so the
        # cron supervisor's exit-status path is the only thing that
        # decides whether to retry.
        _record_job_run("meter_usage", "error")
        raise
    finally:
        # Release explicitly so the lock is freed even if Python is
        # holding the connection in a pool. ``lock_conn.close()``
        # below would also release it on connection-close, but the
        # explicit unlock is cheap and gives clearer log lines if
        # something goes wrong.
        try:
            _release_advisory_lock(lock_conn)
        finally:
            lock_conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_engine_from_env() -> Engine:
    """Build a sync SQLAlchemy engine for the background worker role.

    Looks up ``BG_WORKER_DATABASE_URL`` first (the canonical prod env
    var — points at the ``bg_worker`` Postgres role). Falls back to
    ``core.settings.Settings().database_url`` with the driver prefix
    rewritten from ``+asyncpg`` to ``+psycopg`` so the async URL used
    by the web server can be reused for local dev.

    Production deployments MUST set ``BG_WORKER_DATABASE_URL`` to a
    role with ``BYPASSRLS`` — see
    ``migrations/versions/0002_bg_worker_role.py``.
    """
    url = os.environ.get("BG_WORKER_DATABASE_URL")
    if not url:
        from core.settings import get_settings

        async_url = get_settings().database_url
        # Rewrite ``postgresql+asyncpg://`` → ``postgresql+psycopg://``.
        if async_url.startswith("postgresql+asyncpg://"):
            url = async_url.replace(
                "postgresql+asyncpg://", "postgresql+psycopg://", 1
            )
        else:
            url = async_url
    return create_engine(url, pool_pre_ping=True, future=True)


def main(argv: Optional[List[str]] = None) -> int:
    """Shell-facing entry point. Returns the process exit code.

    Exit 0 on normal completion (including partial failures — the
    per-bucket transaction model means a Stripe outage affects the
    errored bucket only). Exit 2 on an unrecoverable startup error
    (bad DB URL, etc).
    """
    parser = argparse.ArgumentParser(prog="jobs.meter_usage")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read events and log would-be Stripe calls, but never "
        "call Stripe or write to the database.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    try:
        engine = _build_engine_from_env()
    except Exception as exc:
        _log_json("ERROR", "engine_init_failed", error=str(exc))
        return 2

    try:
        stats = run(engine=engine, dry_run=args.dry_run)
    except Exception as exc:  # pragma: no cover - top-level guard
        _log_json("ERROR", "run_crashed", error=str(exc))
        return 1
    finally:
        engine.dispose()

    _log_json("INFO", "run_complete", **stats)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
