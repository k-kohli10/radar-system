"""E2E fixtures. The machinery lives in :mod:`tests.e2e.harness`; this file is fixtures.

``pipeline`` is the whole in-process pipeline — four real services, the real
outbox-worker, and a mock llm-gateway on a real socket — assembled fresh per test. See
the harness module docstring for what is real, what is simplified, and why.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from radar_database import Database
from radar_testing.postgres import database_url, db  # noqa: F401  (shared fixtures)

from tests.e2e.harness import Pipeline, build_live_pipeline, build_pipeline


@pytest_asyncio.fixture
async def pipeline(
    db: Database,  # noqa: F811  (the shared fixture, used here)
    database_url: str,  # noqa: F811  (the shared fixture, used here)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    async with build_pipeline(db, database_url, tmp_path, monkeypatch) as p:
        yield p


@pytest_asyncio.fixture
async def live_pipeline(
    db: Database,  # noqa: F811  (the shared fixture, used here)
    database_url: str,  # noqa: F811  (the shared fixture, used here)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    """The live pipeline. Skips unless ``OPENAI_API_KEY`` is set — see the live test.

    Belt to the ``live`` marker's suspenders: the marker keeps live tests out of the
    default run, and this skip means that even an explicit ``pytest -m live`` without a
    key is a clean skip, never a failure. The key is read from the environment for the
    test's convenience; the gateway's own Vault secret-file loading is tested elsewhere.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set; the live e2e is opt-in")
    async with build_live_pipeline(
        db, database_url, tmp_path, monkeypatch, openai_api_key=api_key
    ) as p:
        yield p
