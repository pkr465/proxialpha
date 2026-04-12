"""Tests for ``jobs.meter_usage`` (Task 05).

These tests exercise the hourly metering job against an in-memory
SQLite database. The job's SQL is dialect-independent (``CURRENT_TIMESTAMP``,
no ``date_trunc``, bucketing in Python) so the same statements run
under both Postgres and SQLite.

We stub the Stripe wire call with a closure that records every
invocation — the job accepts a ``stripe_reporter`` parameter precisely
so this works. Nothing in the Stripe SDK is touched by this suite.

Covered tests (7):

1. ``test_empty_run_is_noop`` — no unreported events → no Stripe calls
2. ``test_groups_by_hour_bucket`` — 3 events in one hour → 1 call
3. ``test_idempotency_key_is_deterministic`` — two runs build same key
4. ``test_marks_reported_on_success`` — reported_at + stripe_usage_record_id set
5. ``test_stripe_error_leaves_rows_unreported`` — reported_at stays NULL on error
6. ``test_skips_events_in_safety_window`` — event <5 min old is skipped
7. ``test_dry_run_does_not_call_stripe_or_mutate`` — dry-run is read-only

These tests use SYNC SQLAlchemy (``create_engine``) because the
metering job itself is sync — we don't want an ``await`` anywhere in
the path and we don't want pytest-asyncio overhead on the suite.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Stripe env vars for the transitive ``api.billing`` imports. The
# metering job itself doesn't import ``api.billing``, but
# ``core.stripe_client`` imports ``core.settings`` which may load a
# real .env. Setting dummies here avoids surprise env pollution.
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_dummy")
os.environ.setdefault("APP_URL", "http://localhost:3000")

from core.stripe_client import StripeReportError  # noqa: E402
from jobs import meter_usage  # noqa: E402


# ---------------------------------------------------------------------------
# Schema — SQLite mirror of the Postgres columns the job touches
# ---------------------------------------------------------------------------

_SCHEMA_SQL = [
    """
    CREATE TABLE organizations (
        id                 TEXT PRIMARY KEY,
        name               TEXT NOT NULL,
        stripe_customer_id TEXT UNIQUE,
        tier               TEXT NOT NULL DEFAULT 'trader',
        created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE subscriptions (
        id                     TEXT PRIMARY KEY,
        org_id                 TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        stripe_subscription_id TEXT UNIQUE NOT NULL,
        stripe_price_id        TEXT NOT NULL,
        status                 TEXT NOT NULL,
        tier                   TEXT NOT NULL,
        seats                  INTEGER NOT NULL DEFAULT 1,
        current_period_start   TEXT NOT NULL,
        current_period_end     TEXT NOT NULL,
        cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,
        metered_item_ids       TEXT NOT NULL DEFAULT '{}',
        created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE usage_events (
        id                     TEXT PRIMARY KEY,
        org_id                 TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        feature                TEXT NOT NULL,
        quantity               INTEGER NOT NULL,
        cost_usd               REAL,
        billed                 INTEGER NOT NULL DEFAULT 0,
        idempotency_key        TEXT NOT NULL UNIQUE,
        stripe_usage_record_id TEXT,
        occurred_at            TEXT NOT NULL,
        reported_at            TEXT
    )
    """,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    """Fresh in-memory SQLite engine with the mirror schema loaded.

    StaticPool keeps every connection pointed at the same in-memory
    database. The metering job opens multiple connections per run
    (one for reads, one per bucket for the write transaction) and
    each needs to see the same rows.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    with eng.begin() as conn:
        for stmt in _SCHEMA_SQL:
            conn.execute(text(stmt))
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def fake_now() -> datetime:
    """A fixed 'current time' the tests pass to ``run(now=...)``.

    Using a fixed datetime (rather than ``datetime.now()``) means the
    safety window, bucket boundaries, and log timestamps are all
    reproducible. The value is chosen so the bucketed events below
    fall cleanly into past hours.
    """
    return datetime(2026, 4, 11, 12, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def seed_org_and_sub(engine: Engine):
    """Create an org + a subscription with a metered_item_ids mapping.

    Returns a dict with:
    * ``org_id`` — str UUID of the org
    * ``si_id`` — the Stripe subscription_item_id for ``signals``
    * ``add_event`` — helper to insert a ``usage_events`` row
    * ``read_event`` — helper to re-read a row by id
    """
    org_id = str(uuid.uuid4())
    si_id = "si_test_signals_abc123"

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO organizations (id, name, tier) "
                "VALUES (:id, :n, 'trader')"
            ),
            {"id": org_id, "n": "Test Org"},
        )
        conn.execute(
            text(
                """
                INSERT INTO subscriptions (
                    id, org_id, stripe_subscription_id, stripe_price_id,
                    status, tier, seats,
                    current_period_start, current_period_end,
                    cancel_at_period_end, metered_item_ids,
                    updated_at
                )
                VALUES (
                    :id, :org_id, :sub_id, 'price_trader_monthly',
                    'active', 'trader', 1,
                    '2026-04-01T00:00:00+00:00', '2026-05-01T00:00:00+00:00',
                    0, :mii,
                    '2026-04-11T00:00:00+00:00'
                )
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": org_id,
                "sub_id": f"sub_{uuid.uuid4().hex[:10]}",
                "mii": json.dumps({"signals": si_id}),
            },
        )

    def _add_event(
        *,
        feature: str,
        quantity: int,
        occurred_at: datetime,
        billed: bool = True,
        reported: bool = False,
        event_id: str = None,
    ) -> str:
        eid = event_id or str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO usage_events (
                        id, org_id, feature, quantity, cost_usd, billed,
                        idempotency_key, occurred_at, reported_at
                    )
                    VALUES (
                        :id, :org_id, :feature, :qty, 0.02, :billed,
                        :idem, :occurred, :reported
                    )
                    """
                ),
                {
                    "id": eid,
                    "org_id": org_id,
                    "feature": feature,
                    "qty": quantity,
                    "billed": 1 if billed else 0,
                    "idem": f"overage_{eid}",
                    "occurred": occurred_at.astimezone(timezone.utc).isoformat(),
                    "reported": None if not reported else "2000-01-01T00:00:00+00:00",
                },
            )
        return eid

    def _read_event(event_id: str) -> Dict[str, Any]:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, reported_at, stripe_usage_record_id "
                    "FROM usage_events WHERE id = :id"
                ),
                {"id": event_id},
            ).fetchone()
        return {
            "id": row[0],
            "reported_at": row[1],
            "stripe_usage_record_id": row[2],
        }

    return {
        "org_id": org_id,
        "si_id": si_id,
        "add_event": _add_event,
        "read_event": _read_event,
    }


def _make_recorder(return_id: str = "mbur_test_1"):
    """Build a Stripe reporter stub that records each call.

    Returned stub appends a dict of kwargs to ``stub.calls`` and
    returns a minimal record dict with an ``id``. Tests inspect
    ``stub.calls`` to assert on what would have been sent to Stripe.
    """

    calls: List[Dict[str, Any]] = []

    def _reporter(
        subscription_item_id: str,
        quantity: int,
        *,
        idempotency_key: str,
        timestamp: int = None,
    ) -> Dict[str, Any]:
        calls.append(
            {
                "subscription_item_id": subscription_item_id,
                "quantity": quantity,
                "idempotency_key": idempotency_key,
                "timestamp": timestamp,
            }
        )
        return {"id": return_id, "quantity": quantity}

    _reporter.calls = calls  # type: ignore[attr-defined]
    return _reporter


def _make_raising_reporter(exc: Exception):
    """Build a stub that raises ``exc`` on every call."""

    def _reporter(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise exc

    return _reporter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_run_is_noop(engine: Engine, fake_now: datetime):
    """With no unreported events, run() makes no Stripe calls and exits clean."""
    reporter = _make_recorder()
    stats = meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter,
    )
    assert reporter.calls == []  # type: ignore[attr-defined]
    assert stats["processed"] == 0
    assert stats["skipped"] == 0
    assert stats["errored"] == 0
    assert stats["buckets"] == 0


def test_groups_by_hour_bucket(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """Three events in the same hour produce a single Stripe call with summed qty.

    Events in a *different* hour produce a second bucket. The test
    places 3 events at 10:05, 10:20, 10:55 (same hour) and 1 event at
    11:30 (next hour), and asserts exactly 2 Stripe calls.
    """
    bucket_a_base = datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc)
    bucket_b_base = datetime(2026, 4, 11, 11, 0, 0, tzinfo=timezone.utc)

    seed_org_and_sub["add_event"](
        feature="signals", quantity=3, occurred_at=bucket_a_base + timedelta(minutes=5)
    )
    seed_org_and_sub["add_event"](
        feature="signals", quantity=5, occurred_at=bucket_a_base + timedelta(minutes=20)
    )
    seed_org_and_sub["add_event"](
        feature="signals", quantity=7, occurred_at=bucket_a_base + timedelta(minutes=55)
    )
    seed_org_and_sub["add_event"](
        feature="signals", quantity=2, occurred_at=bucket_b_base + timedelta(minutes=30)
    )

    reporter = _make_recorder()
    stats = meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter,
    )

    # Two buckets → two Stripe calls. Bucket A sum=15, Bucket B sum=2.
    calls = reporter.calls  # type: ignore[attr-defined]
    assert len(calls) == 2
    quantities = sorted(c["quantity"] for c in calls)
    assert quantities == [2, 15]
    assert stats["processed"] == 2
    assert stats["buckets"] == 2


def test_idempotency_key_is_deterministic(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """The same bucket must produce the same idempotency key on every run.

    This is load-bearing for retry correctness: Stripe deduplicates
    by the key, so a second run of the same bucket MUST present the
    same string or we'd double-bill.
    """
    bucket_base = datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc)
    org_id = seed_org_and_sub["org_id"]

    seed_org_and_sub["add_event"](
        feature="signals", quantity=4, occurred_at=bucket_base + timedelta(minutes=10)
    )
    seed_org_and_sub["add_event"](
        feature="signals", quantity=6, occurred_at=bucket_base + timedelta(minutes=40)
    )

    # First run: collect the key.
    reporter1 = _make_recorder(return_id="mbur_1")
    meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter1,
    )
    assert len(reporter1.calls) == 1  # type: ignore[attr-defined]
    first_key = reporter1.calls[0]["idempotency_key"]  # type: ignore[attr-defined]

    # Reset reported_at so the second run can see the same rows again.
    with engine.begin() as conn:
        conn.execute(text("UPDATE usage_events SET reported_at = NULL"))

    reporter2 = _make_recorder(return_id="mbur_2")
    meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter2,
    )
    assert len(reporter2.calls) == 1  # type: ignore[attr-defined]
    second_key = reporter2.calls[0]["idempotency_key"]  # type: ignore[attr-defined]

    assert first_key == second_key
    # Also assert the format matches the spec.
    bucket_epoch = int(bucket_base.timestamp())
    assert first_key == f"usage_{org_id}_signals_{bucket_epoch}"


def test_marks_reported_on_success(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """After a successful run, the event row has reported_at and stripe id set."""
    bucket_base = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
    eid = seed_org_and_sub["add_event"](
        feature="signals",
        quantity=1,
        occurred_at=bucket_base + timedelta(minutes=10),
    )

    before = seed_org_and_sub["read_event"](eid)
    assert before["reported_at"] is None
    assert before["stripe_usage_record_id"] is None

    reporter = _make_recorder(return_id="mbur_success")
    stats = meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter,
    )

    after = seed_org_and_sub["read_event"](eid)
    assert after["reported_at"] is not None
    assert after["stripe_usage_record_id"] == "mbur_success"
    assert stats["processed"] == 1
    assert stats["errored"] == 0


def test_stripe_error_leaves_rows_unreported(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """A Stripe 500 (or any StripeReportError) leaves the rows for retry.

    The invariant is: a bucket that fails MUST end the run still
    having ``reported_at IS NULL`` so the next cron tick will retry
    with the same idempotency key and reconcile.
    """
    bucket_base = datetime(2026, 4, 11, 8, 0, 0, tzinfo=timezone.utc)
    eid = seed_org_and_sub["add_event"](
        feature="signals",
        quantity=3,
        occurred_at=bucket_base + timedelta(minutes=15),
    )

    reporter = _make_raising_reporter(
        StripeReportError("simulated 500", status=500)
    )
    stats = meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter,
    )

    row = seed_org_and_sub["read_event"](eid)
    assert row["reported_at"] is None
    assert row["stripe_usage_record_id"] is None
    assert stats["processed"] == 0
    assert stats["errored"] == 1


def test_skips_events_in_safety_window(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """Events younger than SAFETY_WINDOW are ignored; older ones processed.

    We add one event 2 minutes before ``fake_now`` (must be skipped)
    and one event 15 minutes before (must be processed).
    """
    fresh = fake_now - timedelta(minutes=2)
    old = fake_now - timedelta(minutes=15)
    fresh_id = seed_org_and_sub["add_event"](
        feature="signals", quantity=99, occurred_at=fresh
    )
    old_id = seed_org_and_sub["add_event"](
        feature="signals", quantity=1, occurred_at=old
    )

    reporter = _make_recorder()
    stats = meter_usage.run(
        engine=engine,
        dry_run=False,
        now=fake_now,
        stripe_reporter=reporter,
    )

    # Exactly one Stripe call: only the old event should have been
    # bucketed. The sum must be 1, not 100 — proving the fresh event
    # was excluded from the SELECT.
    assert len(reporter.calls) == 1  # type: ignore[attr-defined]
    assert reporter.calls[0]["quantity"] == 1  # type: ignore[attr-defined]

    # And the rows reflect the split: old is reported, fresh is not.
    assert seed_org_and_sub["read_event"](old_id)["reported_at"] is not None
    assert seed_org_and_sub["read_event"](fresh_id)["reported_at"] is None
    assert stats["processed"] == 1


def test_dry_run_does_not_call_stripe_or_mutate(
    engine: Engine, fake_now: datetime, seed_org_and_sub
):
    """``dry_run=True`` emits logs but touches neither Stripe nor the DB.

    A dry-run must be safe to execute against a production snapshot
    — no Stripe calls, no reported_at updates, no stripe_usage_record_id
    writes. The run counts the bucket as ``processed`` for reporting
    purposes but this does NOT imply any external side effect.
    """
    bucket_base = datetime(2026, 4, 11, 7, 0, 0, tzinfo=timezone.utc)
    eid = seed_org_and_sub["add_event"](
        feature="signals",
        quantity=4,
        occurred_at=bucket_base + timedelta(minutes=20),
    )

    reporter = _make_recorder()
    stats = meter_usage.run(
        engine=engine,
        dry_run=True,
        now=fake_now,
        stripe_reporter=reporter,
    )

    # No Stripe calls at all.
    assert reporter.calls == []  # type: ignore[attr-defined]
    # Row is still unreported.
    row = seed_org_and_sub["read_event"](eid)
    assert row["reported_at"] is None
    assert row["stripe_usage_record_id"] is None
    # But the stats report the bucket as "processed" (would-have-been).
    assert stats["dry_run"] is True
    assert stats["processed"] == 1
    assert stats["buckets"] == 1
