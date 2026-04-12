"""initial schema: orgs, users, subs, entitlements, usage, agents, stripe_events

Revision ID: 0001
Revises:
Create Date: 2026-04-11 00:00:00+00:00

This migration creates the Phase 1 + Phase 2 control-plane schema:

    public.organizations        (tenant root, RLS on id)
    public.users                (RLS on org_id)
    public.subscriptions        (RLS on org_id)
    public.entitlements         (RLS on org_id; unique (org_id, feature, period_start))
    public.usage_events         (RLS on org_id)
    public.agents               (RLS on org_id; Phase 2, included now per task 01)
    billing_raw.stripe_events   (no RLS; background-worker only)

All tenant-scoped tables share the canonical ADR-005 pattern:

    ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON <t>
      USING (org_id = current_setting('app.current_org_id', true)::uuid);

The ``true`` second argument to ``current_setting`` makes the GUC return
NULL (instead of raising) when unset, so an un-authenticated query sees
zero rows rather than erroring out.

Enum-like columns (tier, status, role, mode, topology) are plain ``text``
with ``CHECK`` constraints — Postgres ENUM types are painful to evolve and
this project will add values frequently during Phase 1–4.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The ``true`` second arg to current_setting() makes it return NULL instead
# of raising when ``app.current_org_id`` is unset. That's deliberate: a
# request that forgets to set the GUC sees nothing rather than crashing.
_TENANT_POLICY_USING = (
    "org_id = current_setting('app.current_org_id', true)::uuid"
)

# For the ``organizations`` table the tenant key is ``id``, not ``org_id``.
_ORG_SELF_POLICY_USING = (
    "id = current_setting('app.current_org_id', true)::uuid"
)


def _enable_rls(table: str, using_clause: str) -> None:
    """Turn on RLS for ``table`` and install the canonical tenant policy."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # FORCE ROW LEVEL SECURITY also applies the policy to the table owner,
    # which keeps migrations from accidentally bypassing RLS in tests run
    # as the superuser. Background workers that need to bypass this must
    # connect as a role with BYPASSRLS.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({using_clause}) "
        f"WITH CHECK ({using_clause})"
    )


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ----- extensions -----
    # gen_random_uuid() ships with pgcrypto on Postgres 13+.
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ----- organizations -----
    op.execute(
        """
        CREATE TABLE organizations (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name               text NOT NULL,
            stripe_customer_id text UNIQUE,
            tier               text NOT NULL DEFAULT 'free'
                                CHECK (tier IN ('free', 'trader', 'pro', 'team')),
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_orgs_stripe_customer ON organizations (stripe_customer_id)"
    )
    _enable_rls("organizations", _ORG_SELF_POLICY_USING)

    # ----- users -----
    op.execute(
        """
        CREATE TABLE users (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email         text NOT NULL UNIQUE,
            org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            role          text NOT NULL DEFAULT 'member'
                           CHECK (role IN ('owner', 'admin', 'member')),
            clerk_user_id text UNIQUE,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_users_org ON users (org_id)")
    _enable_rls("users", _TENANT_POLICY_USING)

    # ----- subscriptions -----
    op.execute(
        """
        CREATE TABLE subscriptions (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                 uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            stripe_subscription_id text UNIQUE NOT NULL,
            stripe_price_id        text NOT NULL,
            status                 text NOT NULL
                                   CHECK (status IN (
                                       'trialing', 'active', 'past_due',
                                       'canceled', 'incomplete', 'incomplete_expired'
                                   )),
            tier                   text NOT NULL
                                   CHECK (tier IN ('trader', 'pro', 'team')),
            seats                  int NOT NULL DEFAULT 1,
            current_period_start   timestamptz NOT NULL,
            current_period_end     timestamptz NOT NULL,
            cancel_at_period_end   boolean NOT NULL DEFAULT false,
            metered_item_ids       jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at             timestamptz NOT NULL DEFAULT now(),
            updated_at             timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_subs_org ON subscriptions (org_id)")
    op.execute("CREATE INDEX idx_subs_status ON subscriptions (status)")
    _enable_rls("subscriptions", _TENANT_POLICY_USING)

    # ----- entitlements -----
    op.execute(
        """
        CREATE TABLE entitlements (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            feature         text NOT NULL,
            period_start    timestamptz NOT NULL,
            period_end      timestamptz NOT NULL,
            included        bigint NOT NULL DEFAULT 0,
            remaining       bigint NOT NULL DEFAULT 0,
            overage_enabled boolean NOT NULL DEFAULT false,
            updated_at      timestamptz NOT NULL DEFAULT now(),
            -- Load-bearing: Phase 1 billing uses ON CONFLICT on this key to
            -- upsert entitlements on webhook replay. Do not weaken.
            CONSTRAINT uq_entitlements_org_feature_period
                UNIQUE (org_id, feature, period_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_ent_org_feature_period "
        "ON entitlements (org_id, feature, period_start DESC)"
    )
    _enable_rls("entitlements", _TENANT_POLICY_USING)

    # ----- usage_events -----
    op.execute(
        """
        CREATE TABLE usage_events (
            id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id                 uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            feature                text NOT NULL,
            quantity               bigint NOT NULL,
            cost_usd               numeric(12, 6),
            billed                 boolean NOT NULL DEFAULT false,
            idempotency_key        text NOT NULL,
            stripe_usage_record_id text,
            occurred_at            timestamptz NOT NULL DEFAULT now(),
            reported_at            timestamptz,
            CONSTRAINT uq_usage_events_idem UNIQUE (idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_usage_org_feature_time "
        "ON usage_events (org_id, feature, occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_usage_unreported "
        "ON usage_events (org_id, feature) "
        "WHERE reported_at IS NULL AND billed = true"
    )
    _enable_rls("usage_events", _TENANT_POLICY_USING)

    # ----- agents (Phase 2, pre-created so Phase 2 needs no new migration) -----
    op.execute(
        """
        CREATE TABLE agents (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id             uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            fingerprint        text NOT NULL,
            hostname           text,
            version            text,
            topology           text NOT NULL DEFAULT 'C'
                                CHECK (topology IN ('A', 'B', 'C')),
            mode               text NOT NULL DEFAULT 'booting'
                                CHECK (mode IN (
                                    'booting', 'running', 'offline_grace',
                                    'degraded', 'revoked', 'stopped'
                                )),
            license_jti        text,
            grace_until        timestamptz,
            last_heartbeat_at  timestamptz,
            last_error         text,
            created_at         timestamptz NOT NULL DEFAULT now(),
            updated_at         timestamptz NOT NULL DEFAULT now(),
            -- A fingerprint is globally unique (sha256 of machine id); we
            -- still include org_id in the UNIQUE so a stolen fingerprint
            -- replayed against a different org cannot collide silently.
            CONSTRAINT uq_agents_org_fingerprint UNIQUE (org_id, fingerprint)
        )
        """
    )
    op.execute("CREATE INDEX idx_agents_org ON agents (org_id)")
    op.execute(
        "CREATE INDEX idx_agents_heartbeat ON agents (last_heartbeat_at DESC)"
    )
    _enable_rls("agents", _TENANT_POLICY_USING)

    # ----- billing_raw.stripe_events (NO RLS) -----
    op.execute("CREATE SCHEMA IF NOT EXISTS billing_raw")
    op.execute(
        """
        CREATE TABLE billing_raw.stripe_events (
            id               text PRIMARY KEY,
            event_type       text NOT NULL,
            received_at      timestamptz NOT NULL DEFAULT now(),
            processed_at     timestamptz,
            payload          jsonb NOT NULL,
            processing_error text
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_stripe_events_unprocessed "
        "ON billing_raw.stripe_events (received_at) "
        "WHERE processed_at IS NULL"
    )
    # Intentionally NOT enabling RLS on billing_raw.stripe_events —
    # it's a raw event log accessed only by a background worker with
    # elevated credentials.


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Drop in reverse dependency order. ``CASCADE`` handles the policies,
    # indexes, and FK constraints.
    op.execute("DROP TABLE IF EXISTS billing_raw.stripe_events CASCADE")
    op.execute("DROP SCHEMA IF EXISTS billing_raw CASCADE")

    op.execute("DROP TABLE IF EXISTS agents CASCADE")
    op.execute("DROP TABLE IF EXISTS usage_events CASCADE")
    op.execute("DROP TABLE IF EXISTS entitlements CASCADE")
    op.execute("DROP TABLE IF EXISTS subscriptions CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS organizations CASCADE")

    # Leave pgcrypto installed — other migrations in the project will
    # depend on gen_random_uuid() and dropping the extension here would
    # require re-adding it on every fresh upgrade.
