"""Elasticsearch implementation of the RADAR traces query contract.

Structural implementation of ``radar_contracts.TraceQuery`` over the
Elasticsearch async client.

This is the **read** side of tracing, the symmetric analog of the logs backend.
Emission happens elsewhere: RADAR services emit via the OpenTelemetry SDK over
OTLP/gRPC to the collector, which forwards to Elasticsearch (ADR 0008 and
``radar_telemetry.tracing``). This backend queries those stored spans back,
fetching every span of one trace by ``correlation_id``, the single join key that
reconstructs one incident's whole path across all services. It creates no index:
the traces data stream and its mapping are owned by the collector's Elasticsearch
exporter.

Where the field names come from
-------------------------------
The document paths are fixed by the OTel collector's Elasticsearch exporter
mapping, configured in ``deploy/otel/`` with ``mapping.mode: otel`` (ADR 0008).
That mode writes the ``traces-generic-default`` data stream and preserves span
attributes under ``attributes.*``, putting ``correlation_id`` at
``attributes.correlation_id`` and the span start at ``@timestamp``, both verified
against a live collector-to-Elasticsearch round trip.

``CORRELATION_ID_FIELD`` is the one canonical spelling of that join-key path,
shared by the exporter config, this backend's default, and the Phase-10 step-10
done-condition test, so a rename surfaces as a broken import or a failing test
rather than a silently-missed trace. The names stay constructor settings so a
different deployment can override them.

POC scope: a correct single-index query returning a whole trace. Connection
pooling, retry-with-jitter, and cross-cluster search are deferred to Phase 13.
"""

from __future__ import annotations

from typing import Any

from elasticsearch import AsyncElasticsearch

BACKEND = "elastic"
"""Registry name this backend registers under for ``TraceQuery``."""

CORRELATION_ID_FIELD = "attributes.correlation_id"
"""Canonical document path of the trace join key.

Shared by the collector's OTel-native exporter mapping (``deploy/otel/``), this
backend's default, and the step-10 done-condition assertion, so the exporter, the
query, and the proof cannot drift out of agreement about where
``correlation_id`` lives.
"""

TRACES_INDEX = "traces-generic-default"
"""Data stream the OTel-native exporter writes traces to (``mapping.mode: otel``)."""

#: Hard cap on spans returned for one trace. Elasticsearch's default ``search``
#: size is 10, and a single incident's trace across eight FastAPI services (each
#: a server span plus its client spans to the gateway and Postgres) can exceed
#: that, so an unset size would silently truncate a trace to its first ten spans.
#: Set explicitly and generously: one incident's trace is bounded.
_MAX_SPANS = 1000


class ElasticTracesBackend:
    """``TraceQuery`` over Elasticsearch, bound to one traces index (pattern)."""

    def __init__(
        self,
        *,
        hosts: str | list[str],
        index: str = TRACES_INDEX,
        api_key: str | None = None,
        correlation_id_field: str = CORRELATION_ID_FIELD,
        timestamp_field: str = "@timestamp",
    ) -> None:
        """Bind to an Elasticsearch cluster and traces index.

        ``hosts`` is one URL or a list of them; ``index`` is the data stream the
        collector's Elasticsearch exporter writes spans to. ``correlation_id_field``
        is the document field carrying the join key and ``timestamp_field`` the
        span start time to order on, both defaulted to the OTel-native mapping the
        collector is configured with (``deploy/otel/``) and overridable for a
        deployment that maps differently.
        """
        self._client = AsyncElasticsearch(hosts=_as_list(hosts), api_key=api_key)
        self._index = index
        self._correlation_id_field = correlation_id_field
        self._timestamp_field = timestamp_field

    async def get_trace(self, correlation_id: str) -> list[dict[str, Any]]:
        """Return every span carrying ``correlation_id``, oldest first.

        One incident's full path is reconstructable from this single value. Spans
        come back in causal order (ascending span start time) so the trace reads
        root to leaf. An unknown id yields an empty list rather than an error.

        The data stream is created lazily by the exporter on the first span, so on
        a brand-new stack that has never received a trace this raises a backend
        "index not found" error rather than returning ``[]``. That is deliberate:
        it fails loud on a missing or misnamed target instead of masking it as an
        empty result. Once any trace has been written, unknown ids return ``[]``.
        """
        response = await self._client.search(
            index=self._index,
            size=_MAX_SPANS,
            sort=[{self._timestamp_field: {"order": "asc"}}],
            query={"term": {self._correlation_id_field: correlation_id}},
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    async def close(self) -> None:
        """Close the underlying Elasticsearch client's connections."""
        await self._client.close()


def _as_list(hosts: str | list[str]) -> list[str]:
    """Normalize a single host URL or a list of them to a list."""
    return [hosts] if isinstance(hosts, str) else list(hosts)
