"""Tests for the Elasticsearch knowledge store.

Two layers, deliberately:

- **Mocked** (default suite): query construction, id keying, error surfacing.
  Cheap, and enough to pin the request shapes.
- **Real Elasticsearch** (``-m infra``): whether Elasticsearch actually ACCEPTS
  the ``dense_vector`` mapping, enforces its dimension, and round-trips a
  document. A mock cannot answer any of those — it returns whatever it is told
  to, so a mapping ES would reject looks identical to one it accepts. Same
  reasoning as verifying database semantics against real Postgres rather than
  against a fake.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore, build_mapping

CLIENT_PATH = "radar_plugin_knowledge_elastic.store.AsyncElasticsearch"

ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")


def _store(**kwargs: Any) -> ElasticKnowledgeStore:
    return ElasticKnowledgeStore(hosts=ES_URL, dims=1536, **kwargs)


def _doc(chunk_id: str, *, dims: int = 1536, **overrides: Any) -> dict[str, Any]:
    document = {
        "chunk_id": chunk_id,
        "runbook_id": "order-service-high-memory",
        "title": "Order Service High Memory",
        "section": "Summary",
        "text": "Memory has stayed above the threshold for five minutes.",
        "embedding": [0.01] * dims,
        "services": ["order-service"],
        "severity": "medium",
        "alert_name": "OrderServiceHighMemory",
        "ordinal": 0,
        "indexed_at": "2026-07-19T12:00:00+00:00",
    }
    document.update(overrides)
    return document


# ------------------------------------------------------------------- mapping


def test_mapping_declares_a_dense_vector_of_the_requested_dimension() -> None:
    mapping = build_mapping(1536)

    vector = mapping["properties"]["embedding"]
    assert vector["type"] == "dense_vector"
    assert vector["dims"] == 1536
    assert vector["index"] is True
    assert vector["similarity"] == "cosine"


def test_mapping_dimension_is_not_hardcoded() -> None:
    """Swapping embedding model must be a config change, not a code edit."""
    assert build_mapping(768)["properties"]["embedding"]["dims"] == 768
    assert build_mapping(3072)["properties"]["embedding"]["dims"] == 3072


def test_text_is_analysed_for_bm25_and_filter_fields_are_not() -> None:
    """`services` pre-filters retrieval; analysing it would break exact terms."""
    properties = build_mapping(1536)["properties"]

    assert properties["text"]["type"] == "text"
    for field in ("services", "severity", "alert_name", "runbook_id", "chunk_id"):
        assert properties[field]["type"] == "keyword", field


def test_mapping_carries_an_indexed_at_date() -> None:
    """Makes incremental indexing observable: after an edit, exactly one
    document carries a newer timestamp. Not for staleness — chunk ids are
    content hashes, so a stored chunk always matches the current file.
    """
    assert build_mapping(1536)["properties"]["indexed_at"] == {"type": "date"}


def test_dims_is_required_with_no_default() -> None:
    """An index created at the wrong dimension rejects every document."""
    with pytest.raises(TypeError):
        ElasticKnowledgeStore(hosts=ES_URL)  # type: ignore[call-arg]


# -------------------------------------------------------------------- mocked


async def test_ensure_index_creates_with_the_mapping_when_absent() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.indices.exists = AsyncMock(return_value=False)
        client.indices.create = AsyncMock()

        created = await _store().ensure_index()

    assert created is True
    kwargs = client.indices.create.await_args.kwargs
    assert kwargs["index"] == "radar-runbooks"
    assert kwargs["mappings"]["properties"]["embedding"]["dims"] == 1536


async def test_ensure_index_is_idempotent_and_never_patches_an_existing_index() -> None:
    """A live dense_vector dimension cannot be changed; do not pretend it can."""
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.indices.exists = AsyncMock(return_value=True)
        client.indices.create = AsyncMock()
        client.indices.put_mapping = AsyncMock()

        created = await _store().ensure_index()

    assert created is False
    client.indices.create.assert_not_awaited()
    client.indices.put_mapping.assert_not_awaited()


async def test_index_keys_documents_by_chunk_id() -> None:
    """chunk_id is a content hash, so re-indexing overwrites instead of duplicating."""
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.bulk = AsyncMock(return_value={"errors": False, "items": []})

        count = await _store().index([_doc("aaa"), _doc("bbb")])

    assert count == 2
    operations = client.bulk.await_args.kwargs["operations"]
    assert operations[0] == {"index": {"_index": "radar-runbooks", "_id": "aaa"}}
    assert operations[2] == {"index": {"_index": "radar-runbooks", "_id": "bbb"}}


async def test_index_rejects_a_document_with_no_chunk_id() -> None:
    """Without an id ES would autogenerate one, silently duplicating on re-index."""
    document = _doc("x")
    del document["chunk_id"]

    with patch(CLIENT_PATH):
        with pytest.raises(ValueError, match="chunk_id"):
            await _store().index([document])


async def test_index_raises_when_elasticsearch_rejects_documents() -> None:
    """A partial bulk failure must not be reported as a successful index."""
    rejection = {
        "errors": True,
        "items": [
            {
                "index": {
                    "error": {
                        "type": "mapper_parsing_exception",
                        "reason": "wrong dimension",
                    }
                }
            }
        ],
    }
    with patch(CLIENT_PATH) as es_cls:
        es_cls.return_value.bulk = AsyncMock(return_value=rejection)

        with pytest.raises(RuntimeError, match="wrong dimension"):
            await _store().index([_doc("aaa")])


async def test_index_of_nothing_makes_no_call() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.bulk = AsyncMock()

        assert await _store().index([]) == 0

    client.bulk.assert_not_awaited()


async def test_chunk_ids_for_returns_stored_ids_without_fetching_sources() -> None:
    """Reconciliation needs ids only; fetching bodies would pull the whole corpus."""
    hits = {"hits": {"hits": [{"_id": "aaa"}, {"_id": "bbb"}]}}
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=hits)

        ids = await _store().chunk_ids_for("order-service-high-memory")

    assert ids == {"aaa", "bbb"}
    kwargs = client.search.await_args.kwargs
    assert kwargs["query"] == {"term": {"runbook_id": "order-service-high-memory"}}
    assert kwargs["source"] is False


async def test_delete_chunks_is_a_no_op_for_an_empty_set() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.bulk = AsyncMock()

        assert await _store().delete_chunks(set()) == 0

    client.bulk.assert_not_awaited()


# ------------------------------------------------------- real Elasticsearch


@pytest.fixture
async def live_store() -> Any:
    """A store bound to a throwaway index on the compose Elasticsearch."""
    store = _store(index="radar-runbooks-test")
    try:
        await store._client.indices.delete(  # noqa: SLF001 - test teardown
            index="radar-runbooks-test", ignore_unavailable=True
        )
    except Exception as exc:  # pragma: no cover - only when ES is absent
        await store.close()
        pytest.skip(f"no Elasticsearch at {ES_URL}: {exc}")
    yield store
    await store._client.indices.delete(  # noqa: SLF001
        index="radar-runbooks-test", ignore_unavailable=True
    )
    await store.close()


@pytest.mark.infra
async def test_real_elasticsearch_accepts_the_dense_vector_mapping(
    live_store: ElasticKnowledgeStore,
) -> None:
    """The question a mock cannot answer: is this mapping actually valid?"""
    assert await live_store.ensure_index() is True
    assert await live_store.verify_dims() == 1536
    assert await live_store.ensure_index() is False  # idempotent against real ES


@pytest.mark.infra
async def test_real_elasticsearch_round_trips_a_chunk_document(
    live_store: ElasticKnowledgeStore,
) -> None:
    await live_store.ensure_index()

    assert await live_store.index([_doc("aaa"), _doc("bbb")], refresh=True) == 2
    assert await live_store.chunk_ids_for("order-service-high-memory") == {"aaa", "bbb"}


@pytest.mark.infra
async def test_real_elasticsearch_upserts_rather_than_duplicating(
    live_store: ElasticKnowledgeStore,
) -> None:
    """Re-indexing an unchanged chunk must not grow the index."""
    await live_store.ensure_index()
    await live_store.index([_doc("aaa")], refresh=True)
    await live_store.index([_doc("aaa")], refresh=True)

    assert await live_store.chunk_ids_for("order-service-high-memory") == {"aaa"}


@pytest.mark.infra
async def test_real_elasticsearch_enforces_the_vector_dimension(
    live_store: ElasticKnowledgeStore,
) -> None:
    """The coupling made visible: a wrong-dimension vector is REJECTED.

    This is why `dims` has no default and why a model swap means a re-index —
    proven against the real engine, not asserted.
    """
    await live_store.ensure_index()

    # Matched on the reason, not just the type: a bare `raises(RuntimeError)`
    # would also pass if the call failed for some unrelated reason, which would
    # make this test look like proof of dimension enforcement without being it.
    with pytest.raises(RuntimeError, match="different number of dimensions"):
        await live_store.index([_doc("wrong", dims=768)], refresh=True)


@pytest.mark.infra
async def test_real_elasticsearch_deletes_stale_chunks(
    live_store: ElasticKnowledgeStore,
) -> None:
    """Reconciliation: a section removed from a runbook leaves the index."""
    await live_store.ensure_index()
    await live_store.index([_doc("aaa"), _doc("bbb")], refresh=True)

    await live_store.delete_chunks({"bbb"}, refresh=True)

    assert await live_store.chunk_ids_for("order-service-high-memory") == {"aaa"}
