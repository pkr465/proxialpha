"""bg_worker role with BYPASSRLS for background jobs

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-11 01:00:00+00:00

Creates the ``bg_worker`` Postgres role that hourly / batch jobs
(starting with :mod:`jobs.meter_usage`) use to connect to the
control-plane database. The role has ``BYPASSRLS`` set so the job can
read and write rows across tenants without having to call
``set_config('app.current_org_id', ...)`` for every tenant in turn.

Why a dedicated role?
---------------------

ADR-005 mandates that RLS is ALWAYS on for tenant-scoped tables and is
enforced via ``FORCE ROW LEVEL SECURITY`` — which means even the
table owner can't see other tenants' rows unless they explicitly
bypass RLS. The only sane way to give batch jobs cross-tenant access
is a separate role with the ``BYPASSRLS`` attribute.

This is strictly *safer* than turning RLS off per-session, because:

* There's no way for a web-server bug to accidentally run without
  RLS — the web server connects as ``proxialpha_app`` which does NOT
  have BYPASSRLS, so any query it makes is policy-gated regardless
  of what code path it goes through.
* Credential scoping: a leaked ``bg_worker`` password compromises
  the metering job's access but does not also compromise the web
  server's auth layer.
* Audit: Postgres ``pg_stat_activity`` shows the role name on every
  connection, so "who wrote this row" is trivially attributable.

Password sourcing — IMPORTANT
-----------------------------

The role's password is read from the ``BG_WORKER_ROLE_PASSWORD`` env
var at migration runtime. There is **no fallback literal** committed
to source control — running the upgrade without the env var set fails
loudly with a clear error. This is deliberate: an earlier revision
of this file shipped a literal placeholder, and any literal a
committer types here ends up in git history forever.

To run a migration locally, export a throwaway value:

    BG_WORKER_ROLE_PASSWORD='dev_only_$(uuidgen)' alembic upgrade head

In production this value must be supplied by your secrets manager
(AWS Secrets Manager, Vault, K8s Secret) at the same time you supply
``BG_WORKER_DATABASE_URL`` to the metering cron — see
``deploy/cron.md``.

Idempotency
-----------

The role creation uses ``CREATE ROLE IF NOT EXISTS``-style guards
(via a DO block) so re-running this migration against a database that
already has the role is a no-op. On a re-run we still ``ALTER ROLE``
the password — that's how rotation works: bump the env var, re-run
the migration, restart the cron.

The ``ALTER ROLE ... BYPASSRLS`` call is idempotent by construction
(setting the attribute that's already set is a no-op in Postgres).
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_BG_WORKER_ROLE = "bg_worker"


def _resolve_password() -> str:
    """Read the bg_worker role password from the environment.

    Fail-closed if the env var is missing — a Postgres role with a
    literal default password is a security incident waiting to happen,
    so we refuse to silently bake one in.
    """
    pw = os.environ.get("BG_WORKER_ROLE_PASSWORD")
    if not pw:
        raise RuntimeError(
            "BG_WORKER_ROLE_PASSWORD env var is required to run migration "
            "0002. Generate a strong random value (e.g. via `openssl rand "
            "-hex 32`) and set it before invoking alembic. See "
            "deploy/cron.md for the production secrets-manager flow."
        )
    # Postgres SQL string literals double single-quotes to escape; do
    # the same here so a value containing apostrophes can't break the
    # generated DDL or smuggle in additional statements.
    return pw.replace("'", "''")


def upgrade() -> None:
    password = _resolve_password()
    # Create the role if it doesn't already exist. We use a PL/pgSQL DO
    # block because Postgres' CREATE ROLE has no IF NOT EXISTS clause.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles
                WHERE rolname = '{_BG_WORKER_ROLE}'
            ) THEN
                CREATE ROLE {_BG_WORKER_ROLE} LOGIN PASSWORD '{password}';
            ELSE
                ALTER ROLE {_BG_WORKER_ROLE} WITH PASSWORD '{password}';
            END IF;
        END
        $$;
        """
    )

    # Give the role BYPASSRLS. This is the load-bearing line: without
    # it, ``jobs.meter_usage`` would see zero rows because
    # ``app.current_org_id`` is never set in a batch context.
    op.execute(f"ALTER ROLE {_BG_WORKER_ROLE} BYPASSRLS")

    # Grant the minimum necessary privileges. The job reads
    # ``usage_events`` + ``subscriptions`` and writes
    # ``usage_events.reported_at``. We also grant on
    # ``organizations`` for future jobs that want to join on it.
    op.execute(
        f"GRANT CONNECT ON DATABASE CURRENT_DATABASE() TO {_BG_WORKER_ROLE}"
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_BG_WORKER_ROLE}")
    op.execute(
        f"GRANT SELECT, UPDATE ON TABLE usage_events TO {_BG_WORKER_ROLE}"
    )
    op.execute(
        f"GRANT SELECT ON TABLE subscriptions, organizations TO {_BG_WORKER_ROLE}"
    )

    # Future tables created in ``public`` should also be visible to
    # bg_worker. ``ALTER DEFAULT PRIVILEGES`` makes that automatic for
    # anything created after this migration by the same role that ran
    # the migration (typically the superuser).
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT ON TABLES TO {_BG_WORKER_ROLE}"
    )


def downgrade() -> None:
    # Revoke privileges first so ``DROP ROLE`` isn't blocked by
    # outstanding grants. We do NOT drop the role on downgrade —
    # dropping a Postgres role that still owns objects (or that is
    # referenced by pg_stat_activity connections) is fragile, and
    # the common downgrade path in ops is just to revert schema
    # changes, not wipe accounts. An operator who really wants to
    # delete the role can do so manually.
    op.execute(
        f"REVOKE SELECT, UPDATE ON TABLE usage_events FROM {_BG_WORKER_ROLE}"
    )
    op.execute(
        f"REVOKE SELECT ON TABLE subscriptions, organizations FROM {_BG_WORKER_ROLE}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {_BG_WORKER_ROLE}")
    op.execute(f"ALTER ROLE {_BG_WORKER_ROLE} NOBYPASSRLS")
