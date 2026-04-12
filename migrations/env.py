"""Alembic environment for the ProxiAlpha control plane.

This file configures Alembic to run async migrations against the Postgres
database defined by ``core.settings.Settings().database_url``.

Design notes:

* The URL is **never** read from ``alembic.ini`` — it comes from the same
  ``Settings`` object the application uses at runtime, so migrations and the
  live app agree on which database they are talking to.
* Autogenerate is deliberately unused for the initial schema; the migration
  is hand-written to get the RLS policies right (Alembic's autogenerate does
  not round-trip ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY``).
* The async hook uses ``async_engine_from_config`` per the Alembic cookbook:
  https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ---------------------------------------------------------------------------
# Path bootstrap: make ``core`` importable when alembic is run from the repo
# root (``alembic upgrade head``) regardless of how the user installed things.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.settings import get_settings  # noqa: E402  (import after sys.path tweak)

# Alembic Config object — gives access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the live database URL. SQLAlchemy 2.x async requires the
# ``postgresql+asyncpg://`` scheme.
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.database_url)

# We do not use SQLAlchemy ORM metadata for this migration; the initial
# schema is raw SQL. Leaving this as ``None`` disables autogenerate, which
# is the correct behaviour for hand-written migrations.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine — useful
    for emitting SQL to stdout without touching a database.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Sync callback handed to the async engine.

    Alembic's migration step itself is synchronous; we just bridge it onto
    the async connection via ``run_sync`` in ``run_async_migrations``.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live DB)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
