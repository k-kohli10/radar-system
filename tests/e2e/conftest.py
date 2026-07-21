"""E2E fixtures. The machinery lives in :mod:`tests.e2e.harness`; this file is fixtures.

``pipeline`` is the whole in-process pipeline — four real services, the real
outbox-worker, and a mock llm-gateway on a real socket — assembled fresh per test. See
the harness module docstring for what is real, what is simplified, and why.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from radar_database import Database
from radar_testing.postgres import database_url, db  # noqa: F401  (shared fixtures)

from tests.e2e.harness import Pipeline, build_live_pipeline, build_pipeline

REPO_TEMPLATES = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "planner-agent"
    / "config"
    / "plan-templates.yaml"
)

PII_TEMPLATE = """
  inventory-service:InventoryCustomerPiiExposedInLogs:
    steps:
      - order: 1
        description: "Customer names, addresses and payment tokens are being
          written to inventory-service debug logs after a log level change"
      - order: 2
        description: "Identify which log sinks received the exposed records
          and purge them"
      - order: 3
        description: "Revert the log level change and confirm PII no longer
          appears in new log lines"
"""


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


@pytest_asyncio.fixture
async def knowledge_pipeline(
    db: Database,  # noqa: F811  (the shared fixture, used here)
    database_url: str,  # noqa: F811  (the shared fixture, used here)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    """The live pipeline PLUS the real knowledge service — the full Phase 8 path.

    Skips without an OpenAI key (like ``live_pipeline``) and without a populated
    Elasticsearch index (retrieval against an empty index would make every
    incident look like no-coverage, and the test would be asserting on an
    unbuilt corpus rather than on grading).
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set; the live e2e is opt-in")

    es_url = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
    try:
        async with httpx.AsyncClient(timeout=5) as probe:
            count = (await probe.get(f"{es_url}/radar-runbooks/_count")).json()["count"]
    except Exception as exc:
        pytest.skip(f"no Elasticsearch at {es_url}: {type(exc).__name__}")
    if not count:
        pytest.skip("radar-runbooks is empty — build the index first")

    # The repo's plan templates, plus one for the no-coverage alert. The added
    # steps are SYMPTOM-RICH on purpose: a measured property of the pipeline
    # (recorded in tests/retrieval/probes.yaml) is that no-coverage detection is
    # reliable for symptom-rich queries and boundary-unstable for alert-shaped
    # queries dominated by the generic `_default` steps — whose "review latency
    # trends" grazes the latency runbook. Giving the runbook-less alert a
    # specific plan template is the realistic deployment answer (plans exist for
    # alerts runbooks do not cover), and it keeps the e2e off the knife-edge.
    templates = REPO_TEMPLATES.read_text() + PII_TEMPLATE
    templates_path = tmp_path / "plan-templates.yaml"
    templates_path.write_text(templates)
    monkeypatch.setenv("RADAR_PLAN_TEMPLATES_PATH", str(templates_path))

    async with build_live_pipeline(
        db,
        database_url,
        tmp_path,
        monkeypatch,
        openai_api_key=api_key,
        with_knowledge=True,
    ) as p:
        yield p
