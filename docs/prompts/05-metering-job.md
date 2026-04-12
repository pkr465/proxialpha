# Task 05 — Hourly Metering Job (Stripe Usage Records)

**Phase:** 1 (Entitlements + Billing)
**Est. effort:** 3 hours
**Prerequisites:** Tasks 01–04.

## Objective

Implement the hourly job that aggregates unreported `usage_events` rows and posts them to Stripe as metered usage records, idempotently.

## Context

- Spec: `docs/specs/phase1-entitlements-and-billing.md` §9.
- ADR-002 idempotency strategy section.
- This job is what turns local usage tracking into actual invoice line items.
- Each tier has a different `overage_price` in Stripe, mapped in `config/tiers.yaml`.

## Exact files to create or modify

1. `jobs/__init__.py` — new package.
2. `jobs/meter_usage.py` — **new file**. Entry point: `python -m jobs.meter_usage`.
3. `core/stripe_client.py` — **new file**. Thin wrapper around `stripe.SubscriptionItem.create_usage_record(...)` with the config and idempotency handling in one place.
4. `tests/test_meter_usage.py` — **new file**.
5. `deploy/cron.md` — **new file**. Documents the hourly cron entry (or Kubernetes CronJob spec) for production.

## Acceptance criteria

`jobs/meter_usage.py`:

1. Opens a DB session, runs:
   ```sql
   SELECT org_id, feature, date_trunc('hour', occurred_at) AS bucket, sum(quantity) AS qty
   FROM usage_events
   WHERE billed = true
     AND reported_at IS NULL
     AND occurred_at < now() - interval '5 minutes'
   GROUP BY org_id, feature, date_trunc('hour', occurred_at)
   ```
   as the `bg_worker` role (bypasses RLS).

2. For each grouped row:
   - Loads the subscription for `org_id`.
   - Finds the metered subscription item ID for the (tier, feature) pair — this is stored on `subscriptions.metered_item_ids` as JSONB `{"signals": "si_..."}`. The webhook handler in Task 02 populates this on subscription creation; add that now if it wasn't added yet.
   - Generates an idempotency key: `usage_{org_id}_{feature}_{bucket_epoch}`.
   - Calls `stripe.SubscriptionItem.create_usage_record(subscription_item_id, quantity=qty, timestamp=bucket_epoch, action='set', idempotency_key=idempotency_key)`.
     - Note: `action='set'` overwrites anything previously sent for that timestamp on that item. This is what you want — it's the safest option if a previous run partially succeeded.
3. On Stripe success, in a single DB transaction:
   - `UPDATE usage_events SET reported_at = now(), stripe_usage_record_id = :id WHERE org_id = ... AND feature = ... AND billed = true AND reported_at IS NULL AND occurred_at >= bucket AND occurred_at < bucket + interval '1 hour'`
4. On Stripe failure, log a structured error with `org_id`, `feature`, `bucket`, and the Stripe error message. Do not mark rows as reported. The next run will try again.
5. The job exits with code 0 on partial success (some orgs succeeded, some failed) so cron doesn't alert on transient upstream flaps. The Prometheus counter `meter_usage_failed_total` is the alerting signal.

Tests:
- `test_meter_usage_empty_run_noop` — no pending rows, no Stripe calls, exit 0.
- `test_meter_usage_groups_correctly` — insert 5 events across 2 hours, assert 2 Stripe calls with correct totals.
- `test_meter_usage_idempotency_key_deterministic` — run the job twice in a row with the same data; second run must use the same idempotency key.
- `test_meter_usage_marks_rows_reported` — after a successful run, rows have `reported_at` and `stripe_usage_record_id` set.
- `test_meter_usage_stripe_error_does_not_mark_reported` — mock Stripe to raise; assert rows stay unreported, job exits 0.
- `test_meter_usage_respects_5min_safety_window` — insert an event 2 minutes old; job skips it (avoids racing the inserter).
- `test_meter_usage_bypasses_rls` — insert events for 3 different orgs; single job run sees all of them.

## Do not

- Do not call `stripe.SubscriptionItem.create_usage_record` with `action='increment'`. Use `'set'`. Increment + retry on a flaky network is a classic double-count pattern.
- Do not run this job more often than hourly. The 5-minute safety window inside the query is designed for an hourly cadence; running more often risks racing the insert path.
- Do not do per-event Stripe calls. Always batch per (org, feature, hour).
- Do not write async code here. This is a batch job; use the sync Stripe SDK, sync SQLAlchemy, and plain Python. Less code, fewer footguns.

## Hints and gotchas

- The `action='set'` idempotency story only works if the idempotency key is deterministic per bucket. If you change the key format in a future release, you may double-count that release's first run. Document this in the job header.
- The `bg_worker` role needs `BYPASSRLS` set via `ALTER ROLE bg_worker BYPASSRLS` in the migration for Task 01 — if you forgot, add it now in a new migration.
- Use `psycopg`'s sync driver here (`psycopg[binary]`), not asyncpg. Easier to reason about transactions in a batch job.
- Log format must be JSON with `ts`, `level`, `job`, `org_id`, `feature`, `bucket`, `qty`, `stripe_id`, `error`. This will be piped to the same observability stack as the web app.

## Test command

```bash
pytest tests/test_meter_usage.py -v
python -m jobs.meter_usage --dry-run  # should print what it would do, no DB writes
```

Both must pass.
