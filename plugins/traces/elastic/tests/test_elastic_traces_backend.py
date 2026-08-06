"""Behavioral tests for the Elasticsearch traces backend (mocked client).

Conformance (``issubclass``) only proves ``ElasticTracesBackend`` is *shaped*
like ``TraceQuery``. These prove it *behaves*: it builds the expected
Elasticsearch query (a term on the correlation-id field), asks for spans in
causal order (ascending span start time), requests a size large enough that a
real trace is never silently truncated to Elasticsearch's default 10, returns an
unknown id as an empty list rather than an error, honors custom field settings,
and surfaces a connection failure rather than swallowing it. No real
Elasticsearch — the async client is mocked, so this is a cheap unit test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from elastic_transport import ConnectionError as ESConnectionError
from radar_plugin_traces_elastic import ElasticTracesBackend
from radar_plugin_traces_elastic.backend import _MAX_SPANS

CLIENT_PATH = "radar_plugin_traces_elastic.backend.AsyncElasticsearch"


def _response(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Shape a minimal ES search response wrapping ``sources`` as hits."""
    return {"hits": {"hits": [{"_source": source} for source in sources]}}


async def test_get_trace_builds_term_query_sorted_ascending_returns_sources() -> None:
    # Two spans of one trace; ES returns them in the asc order we asked for.
    spans = [
        {"name": "POST /events", "@timestamp": "2026-07-09T10:00:00.000Z"},
        {"name": "POST /v1/complete", "@timestamp": "2026-07-09T10:00:01.000Z"},
    ]
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response(spans))
        backend = ElasticTracesBackend(hosts="http://es:9200")
        result = await backend.get_trace("corr-123")

    client.search.assert_awaited_once()
    kwargs = client.search.call_args.kwargs
    assert kwargs["index"] == "traces-apm-*"
    assert kwargs["query"] == {"term": {"Attributes.correlation_id": "corr-123"}}
    # Causal order: root span first, so the trace reads root to leaf.
    assert kwargs["sort"] == [{"@timestamp": {"order": "asc"}}]
    # The anti-truncation guard: never the ES default of 10.
    assert kwargs["size"] == _MAX_SPANS
    assert _MAX_SPANS > 10
    # Spans returned in ES order (the sort already asked oldest-first).
    assert result == spans


async def test_unknown_correlation_id_returns_empty_list() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response([]))
        backend = ElasticTracesBackend(hosts="http://es:9200")
        result = await backend.get_trace("no-such-trace")

    # An id with no spans is an empty trace, not an error.
    assert result == []


async def test_custom_field_settings_are_honored() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(return_value=_response([]))
        backend = ElasticTracesBackend(
            hosts="http://es:9200",
            index="otel-traces",
            correlation_id_field="labels.correlation_id",
            timestamp_field="start_time",
        )
        await backend.get_trace("corr-9")

    kwargs = client.search.call_args.kwargs
    assert kwargs["index"] == "otel-traces"
    assert kwargs["query"] == {"term": {"labels.correlation_id": "corr-9"}}
    assert kwargs["sort"] == [{"start_time": {"order": "asc"}}]


async def test_connection_failure_propagates() -> None:
    with patch(CLIENT_PATH) as es_cls:
        client = es_cls.return_value
        client.search = AsyncMock(side_effect=ESConnectionError("no route to host"))
        backend = ElasticTracesBackend(hosts="http://es:9200")
        # A backend that is down must surface the error, not swallow it into [].
        with pytest.raises(ESConnectionError):
            await backend.get_trace("corr-123")
