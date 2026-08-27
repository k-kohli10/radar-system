"""Elasticsearch implementation of the RADAR logs backend contract.

Structural implementation of ``radar_contracts.LogsBackend`` over the
Elasticsearch async client.

RADAR services emit one JSON log object per line (see ``radar_common.logging``):
``service``, ``event`` (the message), ``level``, ``timestamp`` (ISO-8601 UTC),
and ``correlation_id``. Fluent Bit ships those to Elasticsearch and this backend
queries them back, filtering by service and time window, optionally free-text
matching the message, newest first. The document field names are constructor
settings (defaulting to that convention) because the logs index mapping is a
deployment concern rather than part of the contract.

POC scope: a correct single-index query. Connection pooling, retry-with-jitter,
and search tuning are deferred to Phase 13.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from elasticsearch import AsyncElasticsearch

BACKEND = "elastic"
"""Registry name this backend registers under for ``LogsBackend``."""


class ElasticLogsBackend:
    """``LogsBackend`` over Elasticsearch, bound to one logs index (pattern)."""

    def __init__(
        self,
        *,
        hosts: str | list[str],
        index: str = "radar-*-logs-*",
        api_key: str | None = None,
        service_field: str = "service.keyword",
        message_field: str = "event",
        timestamp_field: str = "timestamp",
    ) -> None:
        """Bind to an Elasticsearch cluster and logs index.

        ``hosts`` is one URL or a list of them; ``index`` is the index or index
        pattern holding the logs. ``service_field``/``message_field``/
        ``timestamp_field`` name the document fields to filter, free-text match,
        and sort on — defaulted to the ``radar_common.logging`` convention
        (``service`` maps to the dynamic ``.keyword`` subfield for exact terms).
        """
        self._client = AsyncElasticsearch(hosts=_as_list(hosts), api_key=api_key)
        self._index = index
        self._service_field = service_field
        self._message_field = message_field
        self._timestamp_field = timestamp_field

    async def query(
        self,
        service_name: str,
        *,
        query: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return matching log entries for ``service_name``, most recent first.

        ``query`` free-text matches the message field; ``start``/``end`` bound
        the time window (either or both may be omitted); ``limit`` caps the
        result count.
        """
        filters: list[dict[str, Any]] = [{"term": {self._service_field: service_name}}]
        window = _time_range(start, end)
        if window is not None:
            filters.append({"range": {self._timestamp_field: window}})

        must: list[dict[str, Any]] = []
        if query:
            must.append({"match": {self._message_field: query}})

        response = await self._client.search(
            index=self._index,
            size=limit,
            sort=[{self._timestamp_field: {"order": "desc"}}],
            query={"bool": {"filter": filters, "must": must}},
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def close(self) -> None:
        """Close the underlying Elasticsearch client's connections."""
        await self._client.close()


def _as_list(hosts: str | list[str]) -> list[str]:
    """Normalize a single host URL or a list of them to a list."""
    return [hosts] if isinstance(hosts, str) else list(hosts)


def _time_range(start: datetime | None, end: datetime | None) -> dict[str, str] | None:
    """Build an ES ``range`` clause from an optional window (``None`` if unbounded)."""
    bounds: dict[str, str] = {}
    if start is not None:
        bounds["gte"] = start.isoformat()
    if end is not None:
        bounds["lte"] = end.isoformat()
    return bounds or None
