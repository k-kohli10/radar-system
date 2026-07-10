"""Shared fixtures for the radar_ingestion test suite.

The dedup-boundary and atomicity tests exercise real Postgres behaviour —
transactions, the dedup window query, deferrable FKs — so they need a running
Postgres. DSN resolution mirrors the ``radar_database`` suite exactly (so both
skip/route identically): ``POSTGRES_TEST_DSN`` if set, otherwise derived from
``POSTGRES_DSN`` (routed to a dedicated ``radar_test`` database so dev data is
never touched), otherwise read from the repo-root ``.env``. If none is
available, or Postgres is unreachable, the suite is skipped.

The schema is built once per session with ``create_all`` and every table is
truncated before each test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from radar_database import Base, Database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DB_NAME = "radar_test"
ASYNC_PREFIX = "postgresql+asyncpg://"


def _dsn_from_env_file() -> str | None:
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.is_file():
        return None
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
    database = Database(database_url)
    await _truncate_all(database)
    try:
        yield database
    finally:
        await database.dispose()
