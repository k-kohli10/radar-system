"""The incremental-pickup done-condition, end to end on real infrastructure.

Real Elasticsearch, real llm-gateway, real OpenAI embeddings, real Postgres
manifest. Nothing is faked, which is the point: every layer below has been proven
in isolation, and this is where they have to work together.

The test makes two claims at once, because either alone would be misleading:

1. **Adding a runbook re-embeds only that runbook.** The count proves the sha256
   change detection is real. A full-rebuild implementation would produce an
   equally correct index and fail this.
2. **A targeted query then retrieves the new content.** Proves the vectors
   actually landed and are searchable — the index is not merely *written* but
   *useful*.

Claim 1 without claim 2 would pass for an indexer that writes nothing at all.
Claim 2 without claim 1 would pass for a full rebuild. Together they say the
thing the phase actually claims: new knowledge becomes retrievable, and it costs
only what changed.

SCOPE NOTE: retrieval here is a direct kNN search rather than the assembled
hybrid pipeline. This test is about INDEXING — that new content lands and is
searchable — so it deliberately does not depend on the retrieval stages, which
are measured separately against the probes in ``tests/retrieval/``.

Requires both markers' prerequisites, so it is opt-in twice over::

    pytest tests/e2e/test_incremental_indexing.py -m "live and infra"
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from radar_database import Database, RunbookDocument
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.indexer import RunbookIndexer
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore
from radar_testing.postgres import database_url, db  # noqa: F401  (shared fixtures)
from sqlalchemy import select

pytestmark = [pytest.mark.live, pytest.mark.infra]

CORPUS = Path(__file__).resolve().parents[2] / "docs" / "runbooks"
ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
#: Defaults to the port `make gateway` serves, so the common local flow needs
#: no override. An EXPLICIT RADAR_GATEWAY_URL that is unreachable FAILS rather
#: than skips — a skip there would read as "opted out" when the truth is
#: "misconfigured", the silent no-op this repo keeps designing against.
GATEWAY_URL = os.environ.get("RADAR_GATEWAY_URL", "http://127.0.0.1:8081")
GATEWAY_URL_EXPLICIT = "RADAR_GATEWAY_URL" in os.environ
#: Per-mode, because knowledge-service holds two gateway tokens: one granting
#: `embed` (this file) and one granting `reason`, which CRAG grading will use.
#: Indexing needs only the first.
TOKEN_FILE = Path.home() / ".radar-dev/secrets/knowledge-service/gateway_token_embed"
TEST_INDEX = "radar-runbooks-e2e"

#: Indexed first. The runbook added mid-test is deliberately NOT in this set.
SEED_RUNBOOKS = (
    "order-service-high-memory",
    "checkout-timeout-rate",
    "payment-gateway-errors",
)
#: Added after the first run. Distinctive content, and deliberately not one half
#: of a confusable pair — this test is about pickup, not disambiguation.
NEW_RUNBOOK = "inventory-oversell-incident"


@pytest_asyncio.fixture
async def embedder() -> AsyncIterator[GatewayEmbeddingClient]:
    """A real gateway client, or skip."""
    if not TOKEN_FILE.exists():
        pytest.skip(f"no knowledge-service gateway token at {TOKEN_FILE}")

    http = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=None)
    try:
        await http.get("/healthz")
    except httpx.HTTPError:
        await http.aclose()
        if GATEWAY_URL_EXPLICIT:
            pytest.fail(
                f"RADAR_GATEWAY_URL was set explicitly but {GATEWAY_URL} does not "
                f"answer — misconfiguration, not an opt-out"
            )
        pytest.skip(f"no llm-gateway at {GATEWAY_URL} (start it with `make gateway`)")

    yield GatewayEmbeddingClient(
        http, SecretStr(TOKEN_FILE.read_text().strip()), dims=1536
    )
    await http.aclose()


@pytest_asyncio.fixture
async def store() -> AsyncIterator[ElasticKnowledgeStore]:
    """A throwaway Elasticsearch index, dropped either side of the test."""
    store = ElasticKnowledgeStore(hosts=ES_URL, dims=1536, index=TEST_INDEX)
    try:
        await store._client.indices.delete(  # noqa: SLF001 - test setup
            index=TEST_INDEX, ignore_unavailable=True
        )
    except Exception as exc:  # pragma: no cover - only when ES is absent
        await store.close()
        pytest.skip(f"no Elasticsearch at {ES_URL}: {exc}")

    yield store

    await store._client.indices.delete(  # noqa: SLF001 - test teardown
        index=TEST_INDEX, ignore_unavailable=True
    )
    await store.close()


def _seed_corpus(tmp_path: Path) -> Path:
    directory = tmp_path / "runbooks"
    directory.mkdir()
    for name in SEED_RUNBOOKS:
        shutil.copy(CORPUS / f"{name}.md", directory / f"{name}.md")
    return directory


async def _knn(
    store: ElasticKnowledgeStore, vector: list[float], *, k: int = 5
) -> list[dict[str, str]]:
    """Direct kNN search — a stand-in until the retrieval slice lands."""
    response = await store._client.search(  # noqa: SLF001
        index=TEST_INDEX,
        knn={
            "field": "embedding",
            "query_vector": vector,
            "k": k,
            "num_candidates": 50,
        },
        size=k,
    )
    return [
        {
            "runbook_id": hit["_source"]["runbook_id"],
            "section": hit["_source"]["section"],
        }
        for hit in response["hits"]["hits"]
    ]


async def test_adding_a_runbook_embeds_only_it_and_makes_it_retrievable(
    db: Database,  # noqa: F811
    tmp_path: Path,
    store: ElasticKnowledgeStore,
    embedder: GatewayEmbeddingClient,
) -> None:
    corpus = _seed_corpus(tmp_path)
    indexer = RunbookIndexer(
        index=store,
        embedder=embedder,
        session_factory=db.session_factory,
        corpus_dir=corpus,
    )

    # --- first run: everything is new ---------------------------------------
    first = await indexer.run()
    assert first.embedded == 24, first  # 3 runbooks x 8 sections
    assert first.processed == 3

    # --- second run, nothing changed: no work at all -------------------------
    second = await indexer.run()
    assert second.embedded == 0, "an unchanged corpus must cost zero embeddings"
    assert second.skipped == 3
    assert second.is_noop

    # --- the new runbook is genuinely absent before we add it ----------------
    query = "we sold more units than we physically have, stock has gone negative"
    before = await _knn(store, (await embedder.embed([query]))[0])
    assert NEW_RUNBOOK not in {hit["runbook_id"] for hit in before}, (
        "the runbook under test was already retrievable, so this test would pass "
        "without proving anything about pickup"
    )

    # --- add one runbook -----------------------------------------------------
    shutil.copy(CORPUS / f"{NEW_RUNBOOK}.md", corpus / f"{NEW_RUNBOOK}.md")

    third = await indexer.run()

    # CLAIM 1: only the new file was embedded.
    assert third.embedded == 8, (
        f"adding one runbook cost {third.embedded} embeddings; only its own 8 "
        f"chunks should have been embedded"
    )
    assert third.processed == 1
    assert third.skipped == 3  # the seed runbooks were untouched
    assert third.deleted == 0

    # CLAIM 2: a targeted query now retrieves it.
    after = await _knn(store, (await embedder.embed([query]))[0])
    retrieved = {hit["runbook_id"] for hit in after}
    assert NEW_RUNBOOK in retrieved, (
        f"the new runbook was indexed but is not retrievable; got {after}"
    )

    # The manifest agrees with what is in the index.
    async with db.session() as session:
        rows = (await session.scalars(select(RunbookDocument))).all()
    recorded = {row.runbook_id: row for row in rows}
    assert set(recorded) == set(SEED_RUNBOOKS) | {NEW_RUNBOOK}
    assert recorded[NEW_RUNBOOK].chunk_count == 8
    assert recorded[NEW_RUNBOOK].services == ["inventory-service"]


async def test_editing_a_section_re_embeds_one_chunk_and_updates_retrieval(
    db: Database,  # noqa: F811
    tmp_path: Path,
    store: ElasticKnowledgeStore,
    embedder: GatewayEmbeddingClient,
) -> None:
    """The other half of incrementality: an edit costs one chunk, and lands."""
    corpus = _seed_corpus(tmp_path)
    indexer = RunbookIndexer(
        index=store,
        embedder=embedder,
        session_factory=db.session_factory,
        corpus_dir=corpus,
    )
    await indexer.run()

    target = corpus / "checkout-timeout-rate.md"
    text = target.read_text()
    anchor = "Page the checkout-service on-call."
    assert anchor in text, f"{anchor!r} missing; this edit would silently do nothing"
    target.write_text(
        text.replace(
            anchor,
            "Page the checkout-service on-call. Escalate to the payments guild "
            "if authorisation latency is implicated.",
            1,
        )
    )

    result = await indexer.run()

    assert result.embedded == 1, f"one edited section cost {result.embedded} embeddings"
    assert result.deleted == 1  # the superseded chunk
    assert result.skipped == 2

    hits = await _knn(
        store, (await embedder.embed(["escalate to the payments guild"]))[0], k=3
    )
    assert any(
        hit["runbook_id"] == "checkout-timeout-rate" and hit["section"] == "Escalation"
        for hit in hits
    ), f"the edited section is not retrievable by its new content; got {hits}"
