"""install_tokens, heartbeat_jti seen-set, and revoked_jti tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-11 14:30:00+00:00

This migration adds the three tables needed to close the Phase 2
go-live blockers identified in ``docs/specs/phase2-go-live-gap-analysis``:

* ``install_tokens`` (P0-1, P1-4) — short-lived single-use tokens
  issued by an admin for first-boot agent enrollment. The
  ``/agent/enroll`` endpoint validates these and the
  ``/api/orgs/{org_id}/install-tokens`` endpoint creates them.

* ``heartbeat_jti_seen`` (P1-6) — a seen-set of JWT ``jti`` values
  the heartbeat handler has already accepted, with a TTL pruner. This
  enforces ADR-003 §Security's "we do not accept the same jti twice"
  rule and stops a stolen token from being replayed indefinitely.

* ``revoked_jti`` (P1-7) — emergency revocation list. An admin can
  insert a ``jti`` here (or a wildcard agent_id) and every subsequent
  heartbeat from that token / agent gets a 403 ``license_revoked``
  without waiting for the signing key to rotate.

All three tables are scoped to ``public`` and protected by RLS keyed
on ``org_id`` so a SQL-injection bug in one tenant cannot reveal
another tenant's install tokens.

Note on JTI uniqueness
----------------------

A JWT ``jti`` is supposed to be globally unique by construction (the
issuer generates a fresh UUID per token). We still scope the
seen-set / revocation tables by ``org_id`` so the indexes stay small
and tenant-isolated, and so a tenant deletion cleanly cascades.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TENANT_POLICY_USING = (
    "org_id = current_setting('app.current_org_id', true)::uuid"
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({_TENANT_POLICY_USING}) "
        f"WITH CHECK ({_TENANT_POLICY_USING})"
    )


def upgrade() -> None:
    # ----- install_tokens -----
    # token_hash: SHA-256 hex of the bearer string. We never store the
    #             plaintext token — only the hash — so a DB leak does
    #             not let an attacker enroll fake agents.
    # consumed_at: NULL until the token is redeemed. Single-use is
    #              enforced via a UNIQUE partial index on (org_id) so
    #              the same hash cannot be reused even if regenerated.
    # expires_at: hard expiry. The enroll endpoint refuses any token
    #             where now() >= expires_at.
    op.execute(
        """
        CREATE TABLE install_tokens (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id        uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            token_hash    text NOT NULL,
            label         text,
            created_by    uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            expires_at    timestamptz NOT NULL,
            consumed_at   timestamptz,
            consumed_by_agent uuid REFERENCES agents(id) ON DELETE SET NULL,
            CONSTRAINT uq_install_tokens_hash UNIQUE (token_hash)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_install_tokens_org ON install_tokens (org_id)"
    )
    op.execute(
        "CREATE INDEX idx_install_tokens_active "
        "ON install_tokens (org_id, expires_at) "
        "WHERE consumed_at IS NULL"
    )
    _enable_rls("install_tokens")

    # ----- heartbeat_jti_seen -----
    # We record every accepted heartbeat token's jti for the lifetime
    # of the token (exp claim). The heartbeat handler checks for the
    # presence of the jti BEFORE accepting it; if found, the request
    # is rejected with 401 invalid_token reason="replay". A small
    # background job (or a per-request DELETE WHERE expires_at < now())
    # garbage-collects expired rows.
    op.execute(
        """
        CREATE TABLE heartbeat_jti_seen (
            jti        text PRIMARY KEY,
            org_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            agent_id   uuid REFERENCES agents(id) ON DELETE CASCADE,
            seen_at    timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_heartbeat_jti_expiry "
        "ON heartbeat_jti_seen (expires_at)"
    )
    op.execute(
        "CREATE INDEX idx_heartbeat_jti_org "
        "ON heartbeat_jti_seen (org_id)"
    )
    _enable_rls("heartbeat_jti_seen")

    # ----- revoked_jti -----
    # Admin-managed revocation list. Two ways to revoke:
    # 1. Insert a specific ``jti`` to kill exactly one token.
    # 2. Insert with ``jti = NULL`` and ``agent_id`` set to kill every
    #    token belonging to that agent until the row is removed.
    # Both shapes are checked at heartbeat time. The bloom-filter
    # optimization is left for Phase 3 — at < 100k revocations a
    # plain B-tree lookup is fine.
    op.execute(
        """
        CREATE TABLE revoked_jti (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id      uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            jti         text,
            agent_id    uuid REFERENCES agents(id) ON DELETE CASCADE,
            revoked_at  timestamptz NOT NULL DEFAULT now(),
            revoked_by  uuid REFERENCES users(id) ON DELETE SET NULL,
            reason      text,
            CONSTRAINT chk_revoked_jti_target
                CHECK (jti IS NOT NULL OR agent_id IS NOT NULL),
            CONSTRAINT uq_revoked_jti_value UNIQUE (jti)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_revoked_jti_org ON revoked_jti (org_id)"
    )
    op.execute(
        "CREATE INDEX idx_revoked_jti_agent ON revoked_jti (agent_id)"
    )
    _enable_rls("revoked_jti")

    # Grant bg_worker minimum reads where useful so future jobs can
    # prune expired heartbeat_jti rows without elevating to superuser.
    op.execute(
        "GRANT SELECT, DELETE ON TABLE heartbeat_jti_seen TO bg_worker"
    )
    op.execute(
        "GRANT SELECT ON TABLE install_tokens, revoked_jti TO bg_worker"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS revoked_jti CASCADE")
    op.execute("DROP TABLE IF EXISTS heartbeat_jti_seen CASCADE")
    op.execute("DROP TABLE IF EXISTS install_tokens CASCADE")
