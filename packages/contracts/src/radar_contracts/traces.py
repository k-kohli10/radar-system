"""Traces backend contracts.

Two vendor-neutral interfaces, split by direction and satisfied by different
components. Both are ``typing.Protocol`` (never ABCs) and reference no vendor
type; the OpenTelemetry SDK lives in the telemetry package, and the Elasticsearch
client lives in ``plugins/traces/elastic/``, not here.

- ``TracesBackend`` is the **emit** side: RADAR runs one span per request, and
  ``start_span`` returns a synchronous context manager whose handle is a
  ``Span`` — attributes known upfront pass via ``attributes``, while those
  discovered during the span (a status code, an exception) are set on the handle
  before the ``with`` block exits. This side is served by the telemetry package's
  OTLP/gRPC path under ADR 0008.

- ``TraceQuery`` is the **read** side: fetch a stored trace's spans back out of
  the backend by ``correlation_id``, the join key that reconstructs one
  incident's whole path across every service.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Span(Protocol):
    """A handle to an in-progress span."""

    def set_attribute(self, key: str, value: str) -> None:
        """Attach a single attribute to the span."""
        ...

    def record_exception(self, exception: BaseException) -> None:
        """Record an exception as an event on the span."""
        ...


@runtime_checkable
class TracesBackend(Protocol):
    """Interface for a tracing backend."""

    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> AbstractContextManager[Span]:
        """Start span ``name`` and return it as a context manager.

        The span ends when the returned context manager exits. Attributes known
        at start may be supplied via ``attributes``; others are set on the
        yielded ``Span`` handle.
        """
        ...


@runtime_checkable
class TraceQuery(Protocol):
    """Interface for reading stored traces back by correlation id."""

    async def get_trace(self, correlation_id: str) -> list[dict[str, Any]]:
        """Return every stored span carrying ``correlation_id``, oldest first.

        ``correlation_id`` is the sole join key (ADR 0008): one incident's full
        path across every service is reconstructable from that single value.
        Spans come back in causal order — ascending start time — so the trace
        reads root to leaf. An unknown id yields an empty list, not an error.

        Spans are plain dictionaries so the contract stays free of any
        backend-specific document shape.
        """
        ...
