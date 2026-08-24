"""Behavioral tests for the Elasticsearch logs backend (mocked client).

Conformance (``issubclass``) only proves ``ElasticLogsBackend`` is *shaped* like
``LogsBackend``. These prove it *behaves*: it builds the expected Elasticsearch
query (service term, optional time range, optional free-text match), calls
``search`` with the right index/size/sort, returns the documents newest-first,
honors custom field settings, and surfaces a connection failure rather than
swallowing it. No real Elasticsearch — the async client is mocked, so this is a
cheap unit test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from elastic_transport import ConnectionError as ESConnectionError
from radar_plugin_logs_elastic import ElasticLogsBackend

CLIENT_PATH = "radar_plugin_logs_elastic.backend.AsyncElasticsearch"


def _response(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape a minimal ES search response wrapping ``sources`` as hits."""
    return {"hits": {"hits": [{"_source": source} for source in sources]}}


async def test_query_builds_full_query_and_returns_sources_newest_first() -> None:
    docs = [
        {"event": "pool exhausted", "timestamp": "2026-07-09T10:02:00+00:00"},
        {"event": "slow query", "timestamp": "2026-07-09T10:01:00+00:00"},
    ]
    start = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    end = datetime(2026, 7, 9, 11, 0, tzinfo=UTC)

    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response(docs))
        backend = ElasticLogsBackend(hosts="http://es:9200", index="radar-*-logs-*")
        result = await backend.query(
            "order-service", query="timeout", start=start, end=end, limit=50
        )

    client.search.assert_awaited_once()
    kwargs = client.search.call_args.kwargs
    assert kwargs["index"] == "radar-*-logs-*"
    assert kwargs["size"] == 50
    assert kwargs["sort"] == [{"timestamp": {"order": "desc"}}]

    bool_query = kwargs["query"]["bool"]
    assert {"term": {"service.keyword": "order-service"}} in bool_query["filter"]
    assert {
        "range": {"timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}
    } in bool_query["filter"]
    assert bool_query["must"] == [{"match": {"event": "timeout"}}]

    # Returns the raw _source docs in ES order (sort already asked newest-first).
    assert result == docs


async def test_query_without_filters_omits_range_and_match() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response([]))
        backend = ElasticLogsBackend(hosts="http://es:9200")
        result = await backend.query("billing")

    kwargs = client.search.call_args.kwargs
    bool_query = kwargs["query"]["bool"]
    # Only the service term — no range clause and no free-text match are added.
    assert bool_query["filter"] == [{"term": {"service.keyword": "billing"}}]
    assert bool_query["must"] == []
    assert kwargs["size"] == 100  # default limit
    assert result == []


async def test_partial_window_emits_only_the_given_bound() -> None:
    start = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response([]))
        backend = ElasticLogsBackend(hosts="http://es:9200")
        await backend.query("billing", start=start)

    bool_query = client.search.call_args.kwargs["query"]["bool"]
    assert {"range": {"timestamp": {"gte": start.isoformat()}}} in bool_query["filter"]


async def test_custom_field_settings_are_honored() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response([]))
        backend = ElasticLogsBackend(
            hosts="http://es:9200",
            service_field="svc",
            message_field="msg",
            timestamp_field="@timestamp",
        )
        await backend.query("checkout", query="oom")

    kwargs = client.search.call_args.kwargs
    bool_query = kwargs["query"]["bool"]
    assert {"term": {"svc": "checkout"}} in bool_query["filter"]
    assert bool_query["must"] == [{"match": {"msg": "oom"}}]
    assert kwargs["sort"] == [{"@timestamp": {"order": "desc"}}]


async def test_connection_failure_propagates() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(side_effect=ESConnectionError("no route to host"))
        backend = ElasticLogsBackend(hosts="http://es:9200")
        # A backend that is down must surface the error, not swallow it into [].
        with pytest.raises(ESConnectionError):
            await backend.query("order-service")
