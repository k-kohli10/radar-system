"""The wiring: does the shell call the right things and combine them honestly?

Fusion is proven in ``test_fusion.py`` and the searches against real
Elasticsearch in the plugin's tests. These do not re-prove either. They prove
the COMPOSITION — that both legs are searched, searched wide, filtered the same
way, and that the fused output is the pure function's answer rather than
something this layer reordered on the way out.
"""

from __future__ import annotations

from typing import Any

import pytest
from radar_knowledge_service.fusion import reciprocal_rank_fusion
from radar_knowledge_service.retrieval import DEFAULT_LEG_SIZE, HybridRetriever

pytestmark = pytest.mark.asyncio

DIMS = 4


def _hit(chunk_id: str, runbook_id: str = "rb", score: float = 1.0) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "runbook_id": runbook_id,
        "section": "Summary",
        "text": f"text for {chunk_id}",
        "score": score,
    }


class FakeBackend:
    """Records calls and returns whatever each leg was told to."""

    def __init__(
        self,
        *,
        bm25: list[dict[str, Any]] | None = None,
        knn: list[dict[str, Any]] | None = None,
    ) -> None:
        self.bm25_result = bm25 or []
        self.knn_result = knn or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search_bm25(
        self, query: str, *, service_name: str | None = None, size: int = 20
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("bm25", {"query": query, "service_name": service_name, "size": size})
        )
        return self.bm25_result

    async def search_knn(
        self,
        vector: list[float],
        *,
        service_name: str | None = None,
        size: int = 20,
        num_candidates: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("knn", {"vector": vector, "service_name": service_name, "size": size})
        )
        return self.knn_result


class FakeEmbedder:
    def __init__(self) -> None:
        self.embedded: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.append(list(texts))
        return [[0.5] * DIMS for _ in texts]


def _retriever(
    backend: FakeBackend, embedder: FakeEmbedder | None = None
) -> HybridRetriever:
    return HybridRetriever(backend=backend, embedder=embedder or FakeEmbedder())


async def test_both_legs_are_searched() -> None:
    """A hybrid that quietly used one leg would still return plausible results."""
    backend = FakeBackend(bm25=[_hit("a")], knn=[_hit("b")])

    await _retriever(backend).retrieve("memory pressure")

    assert [name for name, _ in backend.calls] == ["bm25", "knn"]


async def test_the_query_is_embedded_once_and_sent_to_the_vector_leg() -> None:
    backend = FakeBackend(knn=[_hit("a")])
    embedder = FakeEmbedder()

    await _retriever(backend, embedder).retrieve("memory pressure")

    assert embedder.embedded == [["memory pressure"]]
    knn_call = next(kwargs for name, kwargs in backend.calls if name == "knn")
    assert knn_call["vector"] == [0.5] * DIMS


async def test_the_lexical_leg_gets_the_raw_query_text() -> None:
    """BM25 keys on the words; sending it a vector or a digest would be useless."""
    backend = FakeBackend(bm25=[_hit("a")])

    await _retriever(backend).retrieve("resident memory heap")

    bm25_call = next(kwargs for name, kwargs in backend.calls if name == "bm25")
    assert bm25_call["query"] == "resident memory heap"


async def test_both_legs_are_searched_wider_than_the_requested_limit() -> None:
    """Fusion needs candidates the caller never sees.

    A chunk ranked 15th by one leg and 3rd by the other can outrank one neither
    led with — but only if both legs were asked for more than `limit`. Searching
    at `limit` would make fusion an identity function over truncated lists.
    """
    backend = FakeBackend(bm25=[_hit("a")], knn=[_hit("a")])

    await _retriever(backend).retrieve("q", limit=5)

    assert all(kwargs["size"] == DEFAULT_LEG_SIZE for _, kwargs in backend.calls)
    assert DEFAULT_LEG_SIZE > 5


async def test_the_service_prefilter_reaches_both_legs() -> None:
    """Filtering one leg only would fuse a filtered list with an unfiltered one."""
    backend = FakeBackend(bm25=[_hit("a")], knn=[_hit("b")])

    await _retriever(backend).retrieve("q", service_name="payment-gateway")

    assert all(
        kwargs["service_name"] == "payment-gateway" for _, kwargs in backend.calls
    )


async def test_the_result_is_exactly_the_pure_fusion_of_the_two_rankings() -> None:
    """The shell must not reorder on the way out.

    Asserting against `reciprocal_rank_fusion` itself rather than a hardcoded
    order: this pins that the ordering comes from the proven function, so a
    change to fusion's rules propagates here instead of silently disagreeing.
    """
    lexical = [_hit("a"), _hit("b"), _hit("c")]
    vectorial = [_hit("c"), _hit("b"), _hit("d")]
    backend = FakeBackend(bm25=lexical, knn=vectorial)

    results = await _retriever(backend).retrieve("q", limit=4)

    expected = reciprocal_rank_fusion(
        [["a", "b", "c"], ["c", "b", "d"]],
        limit=4,
    )
    assert [hit["chunk_id"] for hit in results] == expected


async def test_a_chunk_only_one_leg_found_still_survives() -> None:
    """Union, end to end: adding a leg must not remove the other's answers."""
    backend = FakeBackend(bm25=[_hit("lexical-only")], knn=[_hit("vector-only")])

    results = await _retriever(backend).retrieve("q")

    assert {hit["chunk_id"] for hit in results} == {"lexical-only", "vector-only"}


async def test_chunk_payloads_survive_fusion() -> None:
    """Fusion carries ids; the caller needs the text.

    A pipeline that fused correctly and returned bare ids would satisfy every
    ordering test while being useless to the reasoner.
    """
    backend = FakeBackend(bm25=[_hit("a", runbook_id="payment-gateway-errors")])

    (result,) = await _retriever(backend).retrieve("q")

    assert result["runbook_id"] == "payment-gateway-errors"
    assert result["text"] == "text for a"


async def test_the_limit_caps_the_result() -> None:
    backend = FakeBackend(
        bm25=[_hit("a"), _hit("b"), _hit("c")],
        knn=[_hit("d"), _hit("e")],
    )

    results = await _retriever(backend).retrieve("q", limit=2)

    assert len(results) == 2


async def test_one_leg_returning_nothing_degrades_to_the_other() -> None:
    """BM25 legitimately matches nothing for a query with no lexical overlap."""
    backend = FakeBackend(bm25=[], knn=[_hit("a"), _hit("b")])

    results = await _retriever(backend).retrieve("q")

    assert [hit["chunk_id"] for hit in results] == ["a", "b"]


async def test_both_legs_empty_returns_nothing_rather_than_failing() -> None:
    assert await _retriever(FakeBackend()).retrieve("q") == []


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
async def test_an_empty_query_is_refused(blank: str) -> None:
    """Embedding "" returns a valid vector pointing somewhere arbitrary.

    The caller would get confidently-ranked nonsense from the vector leg and
    silence from the lexical one — a wrong answer rather than a visible failure.
    """
    backend = FakeBackend()

    with pytest.raises(ValueError, match="empty query"):
        await _retriever(backend).retrieve(blank)

    assert backend.calls == []
