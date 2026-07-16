"""E2E fixtures. The machinery lives in :mod:`tests.e2e.harness`; this file is fixtures.

``pipeline`` is the whole in-process pipeline — four real services, the real
outbox-worker, and a mock llm-gateway on a real socket — assembled fresh per test. See
the harness module docstring for what is real, what is simplified, and why.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from radar_database import Database
from radar_testing.postgres import database_url, db  # noqa: F401  (shared fixtures)

from tests.e2e.harness import Pipeline, build_pipeline


@pytest_asyncio.fixture
async def pipeline(
    db: Database,  # noqa: F811  (the shared fixture, used here)
    database_url: str,  # noqa: F811  (the shared fixture, used here)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    async with build_pipeline(db, database_url, tmp_path, monkeypatch) as p:
        yield p
