"""Hybrid retrieval: the layer that composes the primitives into an answer.

This is where ``KnowledgeStore.retrieve`` conformance lives. It cannot live on
the Elasticsearch plugin: ``retrieve`` takes a query STRING, so something must
embed it, and a plugin holds no embedding client by design — the gateway is the
only component with provider keys. This layer has the embedder, so it is the
layer that can honestly satisfy the contract.

WHAT IT DOES, AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------
Embed the query, run both searches, fuse their rankings. Every decision inside
that sequence already lives somewhere pure and tested: fusion in
:mod:`radar_knowledge_service.fusion`, query assembly in
:mod:`radar_knowledge_service.query`. This module chooses nothing on its own — it
is the I/O shell, matching how :mod:`radar_knowledge_service.indexer` sits over
``reconciliation``.

Cross-encoder reranking and CRAG grading are later stages and are NOT here. What
this returns is the fused top-``limit``, which the retrieval baseline measures
directly — so the numbers in ``tests/retrieval/baseline.json`` describe this
code, not an aspirational pipeline.

WHY THE LEGS ARE SEARCHED WIDER THAN THE RESULT
-----------------------------------------------
Each leg returns ``leg_size`` (20) candidates and fusion keeps ``limit``. The
width is the point: a chunk ranked 15th by vector search and 3rd by BM25 can
outrank one that neither ranked first, and that outcome only exists if both legs
were asked for more than the caller wants. Searching each leg at ``limit`` would
make fusion nearly an identity function over two already-truncated lists.
"""

from __future__ import annotations

from typing import Any, Protocol

from radar_common import get_logger

from radar_knowledge_service.fusion import reciprocal_rank_fusion

log = get_logger("knowledge.retrieval")

#: Candidates requested from each leg before fusion. Matches the retrieval
#: strategy in the implementation plan (BM25 top 20, kNN top 20, RRF top 10).
DEFAULT_LEG_SIZE = 20

#: Candidates fusion hands to reranking. The plan's "RRF -> top 10, rerank ->
#: top 5": reranking sees more than the caller asked for, because a chunk it
#: would promote to first has to be in front of it to be promoted at all.
DEFAULT_FUSE_SIZE = 10


class SearchBackend(Protocol):
    """The two search primitives, as this layer needs them.

    A Protocol rather than the concrete Elasticsearch class, so this module holds
    no vendor dependency — the plugin satisfies it structurally, exactly as
    ``KnowledgeIndex`` is satisfied on the write side.
    """

    async def search_bm25(
        self, query: str, *, service_name: str | None = ..., size: int = ...
    ) -> list[dict[str, Any]]: ...

    async def search_knn(
        self,
        vector: list[float],
        *,
        service_name: str | None = ...,
        size: int = ...,
        num_candidates: int | None = ...,
    ) -> list[dict[str, Any]]: ...


class QueryEmbedder(Protocol):
    """Just the part of the gateway client retrieval uses."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """The rerank stage, as this layer needs it.

    Returns candidates reordered and never raises: reranking improves an ordering
    that is already usable, so its failure degrades to the fused result rather
    than costing the incident its context.
    """

    async def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, limit: int | None = ...
    ) -> list[dict[str, Any]]: ...


class HybridRetriever:
    """Retrieves runbook chunks by fusing lexical and vector search."""

    def __init__(
        self,
        *,
        backend: SearchBackend,
        embedder: QueryEmbedder,
        reranker: Reranker | None = None,
        leg_size: int = DEFAULT_LEG_SIZE,
        fuse_size: int = DEFAULT_FUSE_SIZE,
    ) -> None:
        """``reranker`` is optional, and its absence is a supported configuration.

        Retrieval without it returns the fused ordering, which is what the
        recorded stage baselines measure. Making it optional is also what lets
        the pipeline be measured at each stage boundary rather than only end to
        end.
        """
        self._backend = backend
        self._embedder = embedder
        self._reranker = reranker
        self._leg_size = leg_size
        self._fuse_size = fuse_size

    async def retrieve(
        self,
        query: str,
        *,
        service_name: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the most relevant chunks for ``query``, best first.

        ``service_name`` pre-filters both legs to one service. It is applied
        inside each search rather than to their results — see the plugin's
        search methods for why post-filtering would starve a leg.

        An empty query is refused rather than sent. Embedding the empty string
        returns a valid vector pointing somewhere arbitrary, and BM25 matches
        nothing, so the caller would get confidently-ranked nonsense from one leg
        and silence from the other.
        """
        if not query.strip():
            raise ValueError("cannot retrieve for an empty query")

        (vector,) = await self._embedder.embed([query])

        lexical = await self._backend.search_bm25(
            query, service_name=service_name, size=self._leg_size
        )
        vectorial = await self._backend.search_knn(
            vector, service_name=service_name, size=self._leg_size
        )

        # Chunks are carried by id through fusion and re-attached after, because
        # fusion is deliberately ignorant of what it is ranking. Where both legs
        # returned the same chunk the payloads are identical (same document, same
        # source fields), so either copy will do; the scores differ and are
        # per-leg, which is why the fused result carries neither.
        by_id: dict[str, dict[str, Any]] = {}
        for hit in (*vectorial, *lexical):
            by_id.setdefault(hit["chunk_id"], hit)

        # Fuse to `fuse_size` rather than to `limit` when a reranker is present:
        # reranking can only reorder what it is given, so truncating to the
        # caller's limit first would hide from it exactly the candidates it might
        # promote. Without a reranker there is nothing downstream to feed, so the
        # fused list is cut to `limit` directly.
        fuse_limit = max(self._fuse_size, limit) if self._reranker else limit
        fused_ids = reciprocal_rank_fusion(
            [
                [hit["chunk_id"] for hit in lexical],
                [hit["chunk_id"] for hit in vectorial],
            ],
            limit=fuse_limit,
        )
        fused = [by_id[chunk_id] for chunk_id in fused_ids]

        log.info(
            "knowledge.retrieved",
            query_chars=len(query),
            service_name=service_name,
            bm25_hits=len(lexical),
            knn_hits=len(vectorial),
            fused=len(fused),
            reranked=self._reranker is not None,
        )

        if self._reranker is None:
            return fused
        return await self._reranker.rerank(query, fused, limit=limit)
