"""Load-suite fixtures. Same in-process pipeline as the e2e suite, at 100x.

``tests/load`` is a sibling of ``tests/e2e``, so it does not inherit the e2e
``pipeline`` fixture — it rebuilds it here from the same :func:`build_pipeline`
machinery. One assembly, driven at scale by the load test.
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
