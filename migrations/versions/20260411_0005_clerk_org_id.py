"""organizations.clerk_org_id for Clerk JIT-provisioning

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-11 15:00:00+00:00

This migration adds the missing piece for the Clerk JWT verifier
middleware (P0-2 in the Phase 2 go-live gap analysis).

The schema baked in 0001 already has ``users.clerk_user_id`` (so we
can resolve a Clerk ``sub`` claim to our internal ``users.id``), but
``organizations`` was missing the equivalent foreign key. Without it,
the JIT-provisioning path in :mod:`api.middleware.clerk_auth` cannot
look up an org by its Clerk org id without a full table scan, and
worse, cannot tell whether an org row should be created or matched
against an existing one when an org is renamed in Clerk.

Why a UNIQUE column rather than a join table?
---------------------------------------------

A Clerk org maps 1:1 to one of our orgs — the column model is the
right shape. We make the column NULLABLE so the existing seed data
(test fixtures, dev orgs created before Clerk integration) keeps
working without backfill. New rows created by the JIT path always
have it set; the verifier refuses to create an org row without one.

RLS impact
----------

None. The new column is plain metadata on a row that is already
RLS-protected via the ``id`` column policy. We do not need to touch
``tenant_isolation`` or any of the existing policies.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``organizations.clerk_org_id`` (nullable, unique)."""
    op.execute(
        """
        ALTER TABLE organizations
            ADD COLUMN IF NOT EXISTS clerk_org_id text
        """
    )
    # Partial unique index — only enforce uniqueness on non-null
    # values so existing pre-Clerk rows (clerk_org_id IS NULL) don't
    # collide with each other.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_clerk_org_id
            ON organizations (clerk_org_id)
            WHERE clerk_org_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_orgs_clerk_org_id")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS clerk_org_id")
