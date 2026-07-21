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

CRAG grading is a later stage and is not here. What this returns is the fused
top-``limit``, which the retrieval baselines measure directly — so the numbers in
``tests/retrieval/`` describe this code, not an aspirational pipeline.

WHY THERE IS NO CROSS-ENCODER RERANK STAGE
-------------------------------------------
There was one. It was built, measured against a criterion pre-registered before
it existed, and removed on the evidence. The full record is in
``tests/retrieval/probes.yaml`` and ``baseline-reranked.json``; in short, at 20
repeats per probe:

- it did not reliably fix either probe it was meant to fix — the depth case
  reached rank 1 in 9 runs of 20, the repair case in 16 of 20;
- it was the ONLY source of run-to-run variance in the pipeline. Filter, kNN and
  fusion return identical ranks on all 17 probes at n=20;
- it destabilised a probe that had been rank 1 at every earlier stage;
- it cost a ``reason``-mode LLM call on every incident.

It did improve the average. That is not the same as improving the system: an
on-call engineer sees ONE retrieval, not a distribution, so an 80% chance of the
right runbook means one incident in five is grounded in the wrong one, varying
between identical alerts on different days. Deterministic-and-slightly-worse
beats better-on-average-but-unpredictable when each incident is a single draw
and the result has to be debuggable.

The baselines and probes stay in the repository deliberately: they are the
evidence for this decision, and deleting them would leave the absence of a
rerank stage looking like an oversight.

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


class HybridRetriever:
    """Retrieves runbook chunks by fusing lexical and vector search."""

    def __init__(
        self,
        *,
        backend: SearchBackend,
        embedder: QueryEmbedder,
        leg_size: int = DEFAULT_LEG_SIZE,
    ) -> None:
        self._backend = backend
        self._embedder = embedder
        self._leg_size = leg_size

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

        fused_ids = reciprocal_rank_fusion(
            [
                [hit["chunk_id"] for hit in lexical],
                [hit["chunk_id"] for hit in vectorial],
            ],
            limit=limit,
        )
        fused = [by_id[chunk_id] for chunk_id in fused_ids]

        log.info(
            "knowledge.retrieved",
            query_chars=len(query),
            service_name=service_name,
            bm25_hits=len(lexical),
            knn_hits=len(vectorial),
            fused=len(fused),
        )
        return fused
