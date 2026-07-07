"""Alembic migration environment (async).

Runs migrations against an async engine built from the ``POSTGRES_DSN``
environment variable (asyncpg driver enforced), diffing against
``radar_database.models.Base.metadata``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from radar_database.connection import ASYNC_DRIVER
from radar_database.models import Base
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("POSTGRES_DSN") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "POSTGRES_DSN is not set; cannot run migrations without a database URL."
        )
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", f"{ASYNC_DRIVER}://", 1)
    return url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(lambda sync_conn: _run(sync_conn))
    await engine.dispose()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
