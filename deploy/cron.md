# Scheduled jobs

This document covers how to run the ProxiAlpha batch jobs on a
schedule. Currently there is one job:

- `jobs.meter_usage` — reports billed overage usage to Stripe.
  Runs hourly.

All jobs are plain Python entry points invoked via `python -m`. They
share no state with the FastAPI web server, so you can run them on
the same image or a stripped-down job-only image — whichever your
ops setup prefers.

## Required environment

Every scheduled job expects these variables to be set in its
execution environment:

| Variable | Purpose |
|---|---|
| `BG_WORKER_DATABASE_URL` | Sync Postgres URL for the `bg_worker` role (`postgresql+psycopg://bg_worker:...@host:5432/db`). The role **must** have `BYPASSRLS` set — see `migrations/versions/20260411_0002_bg_worker_role.py`. |
| `STRIPE_SECRET_KEY` | Server-side Stripe API key (`sk_live_...` in prod, `sk_test_...` in staging). |

Optional overrides:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | — | Fallback if `BG_WORKER_DATABASE_URL` is unset. The job automatically rewrites the `+asyncpg` prefix to `+psycopg` for the sync driver. Not recommended in prod — use `BG_WORKER_DATABASE_URL` explicitly so credential scoping is obvious. |

## Running `jobs.meter_usage`

### Invocation

```sh
python -m jobs.meter_usage            # normal: calls Stripe, writes DB
python -m jobs.meter_usage --dry-run  # read-only: logs would-be calls
```

Exit codes:
- `0` — run completed (per-bucket errors are logged but not fatal)
- `1` — uncaught exception inside `run()`
- `2` — engine / startup failure (bad DB URL, etc)

### Schedule

The job is designed to run **once an hour**, at any minute. Because
the job's own 5-minute safety window already excludes in-flight
events, you have a flexible window within each hour. The canonical
schedule is `17 * * * *` — 17 minutes past the hour — which gives
up to 12 minutes of clock slew headroom without overlapping with
the next hour's events.

### crontab (bare VM)

```cron
# ProxiAlpha — meter overage usage to Stripe
17 * * * * cd /srv/proxialpha && /srv/proxialpha/venv/bin/python -m jobs.meter_usage >> /var/log/proxialpha/meter_usage.log 2>&1
```

Make sure the cron user has access to both the repo checkout and
the log directory. Rotate logs with `logrotate` — the job writes
one JSON line per bucket and runs every hour, so log volume is
bounded by the number of paying overage customers.

### Kubernetes CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: proxialpha-meter-usage
  namespace: proxialpha
spec:
  schedule: "17 * * * *"
  # Never run two instances at once. The job is idempotent under
  # concurrent execution, but concurrent runs produce duplicate
  # log lines which confuses on-call. Forbid is the safe default.
  concurrencyPolicy: Forbid
  # Keep only the last few for debugging; Loki has the full history.
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  startingDeadlineSeconds: 600
  jobTemplate:
    spec:
      # If the job exits non-zero (e.g. DB unreachable), retry once
      # after 30s. If it fails twice, page on-call — something is
      # wrong with the worker's environment.
      backoffLimit: 1
      activeDeadlineSeconds: 1800  # 30 min hard stop
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: meter-usage
              image: ghcr.io/proxiant/proxialpha:latest
              command: ["python", "-m", "jobs.meter_usage"]
              env:
                - name: BG_WORKER_DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: proxialpha-bg-worker
                      key: database_url
                - name: STRIPE_SECRET_KEY
                  valueFrom:
                    secretKeyRef:
                      name: proxialpha-stripe
                      key: secret_key
              resources:
                requests:
                  cpu: "100m"
                  memory: "128Mi"
                limits:
                  cpu: "500m"
                  memory: "256Mi"
```

### systemd timer (alternative to cron)

```ini
# /etc/systemd/system/proxialpha-meter-usage.service
[Unit]
Description=ProxiAlpha meter_usage — report overage to Stripe
After=network-online.target

[Service]
Type=oneshot
User=proxialpha
WorkingDirectory=/srv/proxialpha
EnvironmentFile=/etc/proxialpha/bg_worker.env
ExecStart=/srv/proxialpha/venv/bin/python -m jobs.meter_usage
```

```ini
# /etc/systemd/system/proxialpha-meter-usage.timer
[Unit]
Description=Run meter_usage hourly

[Timer]
OnCalendar=hourly
# Add a 17-minute delay so we run at :17 past the hour, matching
# the canonical cron schedule.
RandomizedDelaySec=0
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
```

Then:

```sh
systemctl daemon-reload
systemctl enable --now proxialpha-meter-usage.timer
systemctl list-timers | grep meter_usage
```

## Observability

The job emits one JSON line per unit of work to stdout. Fields:

| Field | Type | Meaning |
|---|---|---|
| `ts` | ISO8601 string | When the log line was emitted |
| `level` | `INFO` / `WARN` / `ERROR` | Standard severity |
| `job` | `meter_usage` | Constant tag for filtering |
| `msg` | string | Short machine-readable tag (see below) |
| `org_id` | UUID string | Org whose bucket this row is about |
| `feature` | string | Feature name (`signals`) |
| `bucket` | int | Unix-epoch start of the hour bucket |
| `qty` | int | Summed quantity for the bucket |
| `stripe_id` | string | Stripe usage_record id (on success) |
| `idempotency_key` | string | Key sent to Stripe |
| `error` | string | Error message (on failure) |

Messages you'll see:

- `no_pending_events` — run had nothing to do. Expected most hours
  early in the month before any overage accrues.
- `reported` — a bucket was successfully reported to Stripe.
- `dry_run` — same as `reported` but in `--dry-run` mode (no write).
- `no_sub_item` — the org has unreported overage but no matching
  metered subscription item. Usually means the subscription was
  canceled between consumption and metering. Requires investigation
  — the overage may need to be written off or manually invoiced.
- `stripe_error` — Stripe rejected the call. The rows remain
  unreported and the next hour's run will retry with the same key.
- `run_complete` — end-of-run summary with the full stats dict.
- `engine_init_failed` — fatal: couldn't build the DB engine.
- `run_crashed` — fatal: unhandled exception inside `run()`.

Suggested alerts:

- **Page** on two consecutive runs where `errored > 0` (sustained
  Stripe error).
- **Warn** on any `no_sub_item` log line — this is rare and should
  be investigated individually.
- **Page** on `engine_init_failed` or `run_crashed`.

## Manual replay

If a cron tick fails and you need to re-run by hand:

```sh
# Dry-run first to verify what would be reported.
python -m jobs.meter_usage --dry-run

# Then run for real.
python -m jobs.meter_usage
```

The job is safe to run at any time. Because idempotency keys are
deterministic, a manual run that races with a cron tick just results
in one of the two landing on the Stripe record and the other
seeing `reported_at IS NOT NULL` and skipping cleanly.

## Rollback

If you need to **undo** a metering run (e.g. a bad deploy reported
wrong quantities):

1. Identify the buckets by their `idempotency_key` in the logs.
2. Delete the corresponding Stripe usage records manually via the
   Stripe dashboard or `stripe.SubscriptionItem.delete_usage_record`.
3. Clear the corresponding rows in the database:

   ```sql
   UPDATE usage_events
   SET reported_at = NULL, stripe_usage_record_id = NULL
   WHERE idempotency_key IN ('usage_...', 'usage_...');
   ```

4. Deploy the fix and re-run `jobs.meter_usage` — the deterministic
   keys mean Stripe will accept the replayed records on the now-
   cleared buckets.

This is a destructive operation. Only the ops lead should perform
it, and only with the relevant Stripe audit trail already saved.
