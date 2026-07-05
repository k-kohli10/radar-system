"""Knowledge and retrieval contracts.

``EmbeddingProvider`` produces vectors for runbook chunks and queries;
``KnowledgeStore`` indexes those chunks and retrieves the most relevant ones for
an incident. Both are ``typing.Protocol`` interfaces (never ABCs) and reference
no vendor type: the OpenAI embedding call and the Elasticsearch client live in
their respective plugins, not here.

Retrieved chunks are returned as plain dictionaries carrying at least
``runbook_id``, ``title``, ``content``, ``score``, and ``grade`` (the shape the
reasoner places in a recommendation's ``context_bundle``), so the contract stays
free of any backend-specific document type.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Interface for an embedding provider plugin.

    Returns one embedding vector per input text, in order.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` into vectors, one per input, order preserved."""
        ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """Interface for a runbook index and retrieval backend."""

    async def index(self, documents: list[dict[str, Any]]) -> int:
        """Index or upsert runbook chunk documents; return the count indexed."""
        ...

    async def retrieve(
        self,
        query: str,
        *,
        service_name: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Retrieve the most relevant chunks for ``query``, highest score first.

        ``service_name`` optionally pre-filters chunks to a single service;
        ``limit`` caps the number of chunks returned.
        """
        ...
