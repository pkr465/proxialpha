"""Async database engine + per-tenant session management.

This module is the ONE place that knows how to open a database session
with the right ``app.current_org_id`` GUC set. Every tenant-scoped query
in the control plane must go through :func:`get_session`.

Design:

* A single process-wide :class:`AsyncEngine` is lazily constructed from
  ``core.settings.Settings().database_url``. The URL is expected to be
  ``postgresql+asyncpg://...``.
* A :class:`contextvars.ContextVar` tracks the current org_id. Middleware
  (or tests) should use :func:`tenant_context` to set it for the scope
  of a request; :func:`get_session` reads the ContextVar at entry time.
* :func:`get_session` yields an :class:`AsyncSession` bound to a fresh
  connection, opens a transaction, and issues
  ``SELECT set_config('app.current_org_id', <org_id>, true)`` before
  handing the session to the caller. The ``true`` third argument makes
  the GUC *transaction-local*, which is the canonical ADR-005 pattern
  and the only correct choice when using PgBouncer in transaction mode.

Bypassing RLS:
    Do not ``SET row_security = off`` per session — that's a footgun.
    Instead, give background workers a Postgres role with ``BYPASSRLS``
    and point them at a separate ``DATABASE_URL``. This module is
    intentionally offered no "bypass" API.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.settings import get_settings

# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """Return (and lazily create) the process-wide async engine."""
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            # pool_pre_ping avoids "server closed the connection unexpectedly"
            # after idle timeouts in cloud Postgres.
            pool_pre_ping=True,
            # Keep pooled connections warm; tune in production.
            pool_size=5,
            max_overflow=10,
            # echo=False — keep SQL out of prod logs; enable per-call for
            # debugging by constructing a separate engine.
            echo=False,
        )
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _engine


async def dispose_engine() -> None:
    """Close all pooled connections. Call during graceful shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


# ---------------------------------------------------------------------------
# Tenant context
# ---------------------------------------------------------------------------

# The ContextVar is set by request middleware (or by tests). Reading it
# returns ``None`` if no tenant context is active — in that case
# :func:`get_session` will refuse to open a session, because any query
# would see zero rows (the RLS policy becomes ``org_id = NULL``) which
# is almost certainly a bug rather than an intended no-op.
_current_org_id: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "proxialpha_current_org_id", default=None
)


def current_org_id() -> Optional[uuid.UUID]:
    """Return the org_id currently bound to this task, or None."""
    return _current_org_id.get()


@asynccontextmanager
async def tenant_context(org_id: uuid.UUID) -> AsyncIterator[None]:
    """Bind ``org_id`` to the current task for the duration of the block.

    Usage::

        async with tenant_context(org_a.id):
            async with get_session() as session:
                ...
    """
    if not isinstance(org_id, uuid.UUID):
        raise TypeError(
            f"tenant_context requires a uuid.UUID, got {type(org_id).__name__}"
        )
    token = _current_org_id.set(org_id)
    try:
        yield
    finally:
        _current_org_id.reset(token)


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_session(
    org_id: Optional[uuid.UUID] = None,
) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession with ``app.current_org_id`` set.

    ``org_id`` may be passed explicitly or inherited from the
    :func:`tenant_context` ContextVar. If neither is set, a
    ``RuntimeError`` is raised rather than silently opening a session
    that would see no rows.

    The implementation opens a transaction and issues
    ``SELECT set_config('app.current_org_id', <uuid>, true)`` — the
    ``true`` third argument (LOCAL) scopes the GUC to this transaction,
    which is the correct choice under connection pooling. When the
    caller exits the ``async with`` cleanly the transaction is
    committed; on exception it is rolled back.
    """
    effective = org_id if org_id is not None else current_org_id()
    if effective is None:
        raise RuntimeError(
            "get_session called without an org_id and no tenant_context "
            "is active. Refusing to open an unscoped session."
        )
    if not isinstance(effective, uuid.UUID):
        raise TypeError(
            f"org_id must be uuid.UUID, got {type(effective).__name__}"
        )

    get_engine()  # ensure engine + sessionmaker exist
    assert _sessionmaker is not None  # set by get_engine()

    session = _sessionmaker()
    try:
        # Begin an explicit transaction so ``set_config(..., true)``
        # applies for the whole scope. SQLAlchemy AsyncSession starts a
        # transaction implicitly on first execute, but we want it under
        # our control so the set_config call is the first statement.
        await session.begin()
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(effective)},
        )
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
    finally:
        await session.close()
