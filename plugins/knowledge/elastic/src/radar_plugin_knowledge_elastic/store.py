"""Elasticsearch implementation of the RADAR knowledge store contract.

This class provides the index's **storage and search primitives**: the mapping,
upserts, the two operations incremental indexing needs to reconcile a runbook's
chunks, and the two searches hybrid retrieval fuses (:meth:`search_bm25` and
:meth:`search_knn`). Each search method is exactly one Elasticsearch call and
makes no decisions.

``KnowledgeStore.retrieve`` is deliberately absent here. It takes a query STRING,
so something must embed it, and this plugin holds no embedding client: the
gateway is the only component with provider keys, and a plugin depends on no
RADAR service. Conformance therefore lives on the knowledge service's retrieval
layer, which has the embedder and composes these primitives (embed the query, run
both searches, fuse the rankings in the service's pure ``fusion`` module). That
keeps the fusion decision out of the vendor shell.

**The vector dimension is the one real coupling to the embedding model.** It is
baked into the ``dense_vector`` mapping at index-creation time and cannot be
changed in place: a model with different dimensions means creating a new index
and re-embedding the whole corpus. It is therefore a required constructor
argument with no default, so switching models is a config change plus a re-index
rather than a silent mismatch between what the mapping expects and what the
gateway returns.

Documents are keyed by ``chunk_id``, which is a content hash (see the knowledge
service's ``chunking`` module), so indexing the same chunk twice is an idempotent
overwrite.
"""

from __future__ import annotations

from typing import Any

from elasticsearch import AsyncElasticsearch

BACKEND = "elastic"
"""Registry name this backend registers under for ``KnowledgeStore``."""

#: Fields carried on every chunk document. ``text`` is the BM25 target and the
#: string that was embedded; ``embedding`` is its vector. The rest are metadata
#: used to pre-filter (``services``) or to build the reasoner's context bundle.
_TEXT_FIELD = "text"
_VECTOR_FIELD = "embedding"
_INDEXED_AT_FIELD = "indexed_at"

#: Fields returned by the search methods. ``embedding`` is deliberately absent:
#: it is 1536 floats per hit that no reader consumes, and shipping it back would
#: dominate the response.
_RETRIEVAL_SOURCE = [
    "runbook_id",
    "title",
    "section",
    _TEXT_FIELD,
    "services",
    "severity",
    "status",
    "alert_name",
    "ordinal",
    _INDEXED_AT_FIELD,
]


def build_mapping(dims: int, *, similarity: str = "cosine") -> dict[str, Any]:
    """Index mapping for runbook chunks: one BM25 text field, one dense vector.

    ``dims`` is the embedding dimension and is fixed for the life of the index.
    ``similarity`` defaults to cosine: RADAR's embeddings arrive unit-normalised,
    so cosine and dot-product rank identically, and cosine stays correct if a
    future model returns un-normalised vectors.

    Metadata fields are ``keyword`` rather than ``text`` because they are
    filtered on exactly (``services`` pre-filters retrieval to one service) and
    must not be analysed into tokens.

    ``indexed_at`` records when a chunk was written, and is stamped **per indexing
    run**: every chunk a run writes carries one identical value. That answers
    "which chunks did run N write" as a single term query, which Postgres cannot
    (``runbook_documents.indexed_at`` is per-runbook), and makes incremental
    indexing visible in Kibana: after a run that edited one section, exactly one
    document carries the newest timestamp. It is deliberately absent from the
    chunk id, so it can never cause churn.
    """
    return {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "runbook_id": {"type": "keyword"},
            "title": {"type": "text"},
            "section": {"type": "keyword"},
            _TEXT_FIELD: {"type": "text"},
            _VECTOR_FIELD: {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": similarity,
            },
            "services": {"type": "keyword"},
            "severity": {"type": "keyword"},
            "status": {"type": "keyword"},
            "alert_name": {"type": "keyword"},
            "ordinal": {"type": "integer"},
            _INDEXED_AT_FIELD: {"type": "date"},
        }
    }


class ElasticKnowledgeStore:
    """Runbook chunk index over Elasticsearch, bound to one index and dimension."""

    def __init__(
        self,
        *,
        hosts: str | list[str],
        dims: int,
        index: str = "radar-runbooks",
        api_key: str | None = None,
        similarity: str = "cosine",
    ) -> None:
        """Bind to an Elasticsearch cluster, an index, and an embedding dimension.

        ``dims`` has no default deliberately: it must match what the gateway's
        configured embedding model returns, and an index created at the wrong
        dimension rejects every document rather than degrading quietly.
        """
        self._client = AsyncElasticsearch(hosts=_as_list(hosts), api_key=api_key)
        self._index = index
        self._dims = dims
        self._similarity = similarity

    @property
    def index_name(self) -> str:
        return self._index

    @property
    def dims(self) -> int:
        return self._dims

    async def close(self) -> None:
        """Release the underlying client's connections."""
        await self._client.close()

    async def ensure_index(self) -> bool:
        """Create the index with its mapping if absent. Returns True if created.

        Idempotent: an existing index is left exactly as it is, never patched.
        A ``dense_vector`` dimension cannot be changed on a live mapping, so
        attempting to reconcile one would either fail or appear to succeed while
        leaving the old dimension in place. Changing dimension is a new index plus
        a re-index; see :meth:`verify_dims`.
        """
        if await self._client.indices.exists(index=self._index):
            return False
        await self._client.indices.create(
            index=self._index,
            mappings=build_mapping(self._dims, similarity=self._similarity),
        )
        return True

    async def verify_dims(self) -> int:
        """Return the dimension the live index was actually created with.

        Catches a model swap that changed dimensions without a re-index: the
        caller compares this against the dimension it expects and refuses to index
        on a mismatch, rather than discovering it one rejected bulk request at a
        time.
        """
        mapping = await self._client.indices.get_mapping(index=self._index)
        properties = mapping[self._index]["mappings"]["properties"]
        return int(properties[_VECTOR_FIELD]["dims"])

    async def index(
        self, documents: list[dict[str, Any]], *, refresh: bool = False
    ) -> int:
        """Upsert chunk documents, keyed by ``chunk_id``. Returns the count indexed.

        ``chunk_id`` is the Elasticsearch ``_id``, and it is a content hash, so
        re-indexing an unchanged chunk overwrites it with identical content
        rather than creating a duplicate.

        ``refresh`` makes the documents immediately searchable. The indexer uses
        it at the end of a run so a query straight afterwards sees new content;
        leaving it False lets Elasticsearch batch refreshes normally.
        """
        if not documents:
            return 0

        operations: list[dict[str, Any]] = []
        for document in documents:
            chunk_id = document.get("chunk_id")
            if not chunk_id:
                raise ValueError("every chunk document must carry a chunk_id")
            operations.append({"index": {"_index": self._index, "_id": chunk_id}})
            operations.append(document)

        raw = await self._client.bulk(operations=operations, refresh=refresh)
        response: dict[str, Any] = dict(raw)
        if response.get("errors"):
            raise RuntimeError(
                f"elasticsearch rejected {_count_failures(response)} of "
                f"{len(documents)} chunk documents: {_first_error(response)}"
            )
        return len(documents)

    async def search_bm25(
        self, query: str, *, service_name: str | None = None, size: int = 20
    ) -> list[dict[str, Any]]:
        """Lexical search over ``text``. Returns hits, best first.

        One of the two legs hybrid retrieval fuses. It contributes what vector
        search structurally cannot: exact term matching. An embedding places a
        query near text with similar MEANING, which is why it confuses runbooks
        describing opposite conditions in the same vocabulary; BM25 keys on the
        distinguishing words themselves.

        ``service_name`` applies the pre-filter as a ``filter`` clause rather
        than a query clause, so it constrains which documents match without
        contributing to the relevance score. A filter that scored would let the
        service term inflate rankings, making a chunk look more relevant for
        naming its own service.
        """
        response = await self._client.search(
            index=self._index,
            query=_with_service_filter(
                {"match": {_TEXT_FIELD: query}}, service_name=service_name
            ),
            source_includes=_RETRIEVAL_SOURCE,
            size=size,
        )
        return _hits(response)

    async def search_knn(
        self,
        vector: list[float],
        *,
        service_name: str | None = None,
        size: int = 20,
        num_candidates: int | None = None,
    ) -> list[dict[str, Any]]:
        """Vector search over ``embedding``. Returns hits, best first.

        The other leg. ``num_candidates`` controls how much of the HNSW graph is
        explored and defaults to ``10 * size``, Elasticsearch's own guidance: the
        graph is approximate, and too few candidates makes results depend on
        which nodes the traversal happened to visit. The retrieval baseline sets
        it to the whole corpus to remove that variable from a measurement (see
        ``scripts/measure-retrieval-baseline.py``).

        The service pre-filter is passed inside the ``knn`` clause rather than
        applied afterwards. Filtering after the fact would search the whole corpus
        and then discard non-matching hits, returning FEWER than ``size`` results
        from a search that had no idea it was being narrowed.
        """
        knn: dict[str, Any] = {
            "field": _VECTOR_FIELD,
            "query_vector": vector,
            "k": size,
            "num_candidates": num_candidates if num_candidates else size * 10,
        }
        if service_name is not None:
            knn["filter"] = {"term": {"services": service_name}}

        response = await self._client.search(
            index=self._index, knn=knn, source_includes=_RETRIEVAL_SOURCE, size=size
        )
        return _hits(response)

    async def chunk_ids_for(self, runbook_id: str) -> set[str]:
        """Chunk ids currently stored for one runbook.

        Incremental indexing compares this against the ids the chunker just
        produced: ids in both are unchanged and need no embedding call, ids only
        here are stale and get deleted.
        """
        # `source=False` is the client's typed spelling of the API's `_source`
        # field: ids only, no document bodies. Fetching bodies here would pull
        # every chunk's full text back just to compare hashes.
        response = await self._client.search(
            index=self._index,
            query={"term": {"runbook_id": runbook_id}},
            source=False,
            size=10_000,
        )
        return {hit["_id"] for hit in response["hits"]["hits"]}

    async def delete_chunks(self, chunk_ids: set[str], *, refresh: bool = False) -> int:
        """Delete chunks by id. Returns the count requested.

        Used to remove sections that no longer exist after a runbook was edited.
        Deleting an id that is already gone is not an error; the desired end state
        is the same either way.
        """
        if not chunk_ids:
            return 0

        operations: list[dict[str, Any]] = [
            {"delete": {"_index": self._index, "_id": chunk_id}}
            for chunk_id in chunk_ids
        ]
        await self._client.bulk(operations=operations, refresh=refresh)
        return len(chunk_ids)


def _as_list(hosts: str | list[str]) -> list[str]:
    return [hosts] if isinstance(hosts, str) else hosts


def _with_service_filter(
    query: dict[str, Any], *, service_name: str | None
) -> dict[str, Any]:
    """Wrap ``query`` so ``service_name`` narrows it without scoring."""
    if service_name is None:
        return query
    return {
        "bool": {
            "must": [query],
            "filter": [{"term": {"services": service_name}}],
        }
    }


def _hits(response: Any) -> list[dict[str, Any]]:
    """Flatten an Elasticsearch response into chunk dicts carrying their score.

    ``chunk_id`` is taken from ``_id`` rather than from ``_source``: they hold the
    same value (documents are keyed by it), and reading the one Elasticsearch
    ranked guarantees the score and the id describe the same document even if a
    source field were ever to drift.
    """
    return [
        {**hit["_source"], "chunk_id": hit["_id"], "score": hit["_score"]}
        for hit in response["hits"]["hits"]
    ]


def _count_failures(response: dict[str, Any]) -> int:
    return sum(1 for item in response.get("items", []) if _item_error(item) is not None)


def _first_error(response: dict[str, Any]) -> str:
    for item in response.get("items", []):
        error = _item_error(item)
        if error is not None:
            # Reason only: ES error bodies echo the document, which for a chunk
            # would put a full runbook section into the log line.
            return str(error.get("reason", error.get("type", "unknown")))
    return "unknown"


def _item_error(item: dict[str, Any]) -> dict[str, Any] | None:
    for action in item.values():
        if isinstance(action, dict) and "error" in action:
            error = action["error"]
            return error if isinstance(error, dict) else {"reason": str(error)}
    return None
