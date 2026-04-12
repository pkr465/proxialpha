"""add agents.last_metrics + config_version columns (heartbeat endpoint)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-11 02:00:00+00:00

Task 06 adds the ``POST /agent/heartbeat`` endpoint. The handler needs
three columns that aren't in the original ``agents`` / ``organizations``
tables from migration 0001:

* ``agents.last_metrics`` (``jsonb``) — agent-reported stats stored
  verbatim on each heartbeat. Opaque to the control plane; read only
  by the observability stack.
* ``agents.config_version`` (``int``) — the config bundle version the
  agent last acknowledged. Used by the handler to decide whether to
  send a fresh config_bundle in the response.
* ``organizations.config_version`` (``int``) — the current config
  bundle version for the org. Incremented whenever an admin pushes
  a new config through (Phase 3 plumbing; Phase 2 just reads it).

All three default to sane zero/empty values so existing rows migrate
cleanly. None of the columns are NULLable to keep the handler code
free of three-valued logic — the default literal is applied via
``server_default`` so a backfill isn't required.

RLS impact
----------

``agents`` and ``organizations`` are already RLS-enabled by 0001.
Adding columns does not change policies — the existing
``tenant_isolation`` policy continues to apply. We don't need to
touch ``bg_worker``'s grants because this migration only adds
columns to tables it already has access to.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- agents.last_metrics ----
    # JSONB in Postgres. The handler stores a JSON-serialised dict
    # on every heartbeat; readers (observability, support) query it
    # with JSONB operators. Default ``'{}'::jsonb`` so backfill is
    # a no-op — existing agents rows get an empty dict.
    op.add_column(
        "agents",
        sa.Column(
            "last_metrics",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # ---- agents.config_version ----
    # Tracks the last config bundle version the agent acknowledged.
    # Starts at 0 so first-heartbeat always gets a bundle if the
    # org has pushed any config at all (Phase 3 sets organizations
    # .config_version = 1 on initial provisioning).
    op.add_column(
        "agents",
        sa.Column(
            "config_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ---- organizations.config_version ----
    op.add_column(
        "organizations",
        sa.Column(
            "config_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    # Columns are additive; removing them is safe provided no
    # dependent views or constraints exist (Phase 2 adds neither).
    op.drop_column("organizations", "config_version")
    op.drop_column("agents", "config_version")
    op.drop_column("agents", "last_metrics")
