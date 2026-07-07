"""Async engine and session factory.

A service creates one :class:`Database` at startup, holding the async engine and
an ``AsyncSession`` factory, and disposes it on shutdown. Sessions are yielded
without an implicit commit so callers control transaction boundaries — essential
for the transactional outbox, where a state change and its outbox event must
commit (or roll back) together.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ASYNC_DRIVER = "postgresql+asyncpg"


def _ensure_async_driver(dsn: str) -> str:
    """Upgrade a bare ``postgresql://`` DSN to the asyncpg driver."""
    if dsn.startswith(f"{ASYNC_DRIVER}://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", f"{ASYNC_DRIVER}://", 1)
    return dsn


def create_engine(
    dsn: str, *, echo: bool = False, pool_pre_ping: bool = True
) -> AsyncEngine:
    """Create an async engine for ``dsn`` (asyncpg driver enforced).

    ``pool_pre_ping`` checks out live connections, transparently replacing ones
    the database dropped — cheap insurance against stale pooled connections.
    """
    return create_async_engine(
        _ensure_async_driver(dsn), echo=echo, pool_pre_ping=pool_pre_ping
    )


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create an ``AsyncSession`` factory bound to ``engine``.

    ``expire_on_commit=False`` so attributes stay usable after commit without a
    lazy (I/O-triggering) refresh.
    """
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Database:
    """Owns the async engine and session factory for one service process."""

    def __init__(self, dsn: str, *, echo: bool = False) -> None:
        self._engine = create_engine(dsn, echo=echo)
        self._session_factory = create_session_factory(self._engine)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session and close it on exit.

        No implicit commit: the caller commits explicitly, and an uncommitted
        session is rolled back on close. Use for full control of the
        transaction boundary::

            async with db.session() as session:
                session.add(incident)
                await write_outbox_event(session, event)
                await session.commit()
        """
        async with self._session_factory() as session:
            yield session

    async def ping(self) -> bool:
        """Return whether the database answers ``SELECT 1`` (for ``/readyz``).

        Deliberately catches every exception: a readiness probe must report
        "not ready" rather than propagate. Connect-time failures (e.g. an
        asyncpg auth error) are not always wrapped as ``SQLAlchemyError``.
        """
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def dispose(self) -> None:
        """Dispose the engine's connection pool (call on shutdown)."""
        await self._engine.dispose()
