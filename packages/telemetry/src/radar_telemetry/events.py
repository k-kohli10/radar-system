"""Span helpers tying correlation ids and domain events to traces.

``correlation_id`` is the join key between RADAR's structured logs and OTel spans
(docs/adr/0008-otel-to-elasticsearch.md): :func:`bind_correlation_id` sets it on
*both* the log context and the current span, so a single value reconstructs an
incident's full path in Kibana across logs and traces.
:func:`record_event` annotates the current span with a domain event.

Both helpers act on the currently active span; if none is active (e.g. work
outside a request) the span calls are safe no-ops.
"""

from __future__ import annotations

from uuid import UUID

from opentelemetry import trace
from opentelemetry.util.types import AttributeValue
from radar_common import CORRELATION_ID_KEY
from radar_common import bind_correlation_id as _bind_log_correlation_id


def bind_correlation_id(correlation_id: UUID | str) -> None:
    """Bind ``correlation_id`` to the log context and the current span.

    Call once per request/event: every subsequent log line carries it (via
    ``radar_common``) and the active span becomes searchable by it in Kibana APM.
    """
    value = str(correlation_id)
    _bind_log_correlation_id(value)
    trace.get_current_span().set_attribute(CORRELATION_ID_KEY, value)


def record_event(name: str, **attributes: AttributeValue) -> None:
    """Record a domain event on the current span (no-op if none is active)."""
    trace.get_current_span().add_event(name, attributes=attributes or None)
