"""Real-Postgres pytest fixtures, shared by every RADAR suite that needs one.

Several of RADAR's guarantees *are* Postgres guarantees — ``FOR UPDATE SKIP
LOCKED`` row locking, transaction-scoped ``NOW()``, deferrable foreign keys, real
concurrent transactions. None of them exist against SQLite and a mock has no
locking at all, so the suites that assert them need a running Postgres and must
skip cleanly when there is none.

This module owns that setup once, for all of them. It was extracted from three
byte-identical copies (``packages/database``, ``apps/ingestion``,
``apps/outbox-worker``) — the duplication note in the ingestion copy called for
exactly this at the third consumer.

DSN resolution, unchanged from the copies it replaces: ``POSTGRES_TEST_DSN`` if
set, otherwise derived from ``POSTGRES_DSN`` (routed to a dedicated
``radar_test`` database so dev data is never touched), otherwise read from the
repo-root ``.env``. If none is available, or Postgres is unreachable, the suite
skips. The schema is built once per session with ``create_all`` and every table
is truncated before each test.

Consumers pull the fixtures into their ``conftest.py``::

    from radar_testing.postgres import database_url, db

    __all__ = ["database_url", "db"]
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from radar_database import Base, Database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB_NAME = "radar_test"
ASYNC_PREFIX = "postgresql+asyncpg://"


def _upwards(start: Path) -> Iterator[Path]:
    base = start if start.is_dir() else start.parent
    yield base
    yield from base.parents


def _dsn_from_env_file() -> str | None:
    """Read ``POSTGRES_DSN`` from the repo-root ``.env``, if there is one.

    Searched upward from the working directory and from this module, so the
    lookup does not depend on where pytest was invoked or how deep the consuming
    suite sits (the copies this replaces each hard-coded their own depth).
    """
    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve()):
        for directory in _upwards(start):
            if directory in seen:
                continue
            seen.add(directory)
            env_path = directory / ".env"
            if not env_path.is_file():
                continue
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if line.startswith("POSTGRES_DSN="):
                    return line.split("=", 1)[1].strip()
    return None


def _normalize(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", ASYNC_PREFIX, 1)
    return dsn


def _resolve_dsns() -> tuple[str, str] | None:
    """Return ``(admin_dsn, test_dsn)`` or ``None`` if nothing is configured."""
    explicit = os.environ.get("POSTGRES_TEST_DSN")
    base = os.environ.get("POSTGRES_DSN") or _dsn_from_env_file()
    if explicit:
        test_dsn = _normalize(explicit)
    elif base:
        prefix = _normalize(base).rsplit("/", 1)[0]
        test_dsn = f"{prefix}/{TEST_DB_NAME}"
    else:
        return None
    admin_dsn = f"{test_dsn.rsplit('/', 1)[0]}/postgres"
    return admin_dsn, test_dsn


@pytest.fixture(scope="session")
def database_url() -> str:
    """Session-scoped DSN for the dedicated test database (skips if unavailable)."""
    dsns = _resolve_dsns()
    if dsns is None:
        pytest.skip("No POSTGRES_DSN/POSTGRES_TEST_DSN configured for database tests")
    admin_dsn, test_dsn = dsns

    async def _setup() -> None:
        admin = create_async_engine(admin_dsn, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"),
                    {"n": TEST_DB_NAME},
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        finally:
            await admin.dispose()
        engine = create_async_engine(test_dsn)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    try:
        asyncio.run(_setup())
    except OSError as exc:  # Postgres not reachable
        pytest.skip(f"Postgres not reachable for database tests: {exc}")
    return test_dsn


async def _truncate_all(database: Database) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with database.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db(database_url: str) -> AsyncIterator[Database]:
    """A ``Database`` on the test DB, truncated before each test."""
    database = Database(database_url)
    await _truncate_all(database)
    try:
        yield database
    finally:
        await database.dispose()
