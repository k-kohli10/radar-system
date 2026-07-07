"""Span-event and correlation-id join-key tests."""

from __future__ import annotations

from uuid import uuid4

import radar_telemetry as rt
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.contextvars import get_contextvars

Tracing = tuple[TracerProvider, InMemorySpanExporter]


def test_bind_correlation_id_sets_span_and_log_context(tracing: Tracing) -> None:
    provider, exporter = tracing
    correlation_id = uuid4()

    with provider.get_tracer("t").start_as_current_span("op"):
        rt.bind_correlation_id(correlation_id)

    span_attrs = exporter.get_finished_spans()[0].attributes
    assert span_attrs is not None
    # Same value lands on both the span and the log context — the join key.
    assert span_attrs["correlation_id"] == str(correlation_id)
    assert get_contextvars()["correlation_id"] == str(correlation_id)


def test_record_event_annotates_current_span(tracing: Tracing) -> None:
    provider, exporter = tracing

    with provider.get_tracer("t").start_as_current_span("op"):
        rt.record_event("incident.opened", incident_id="abc", severity="critical")

    span = exporter.get_finished_spans()[0]
    events = {event.name: dict(event.attributes or {}) for event in span.events}
    assert "incident.opened" in events
    assert events["incident.opened"]["severity"] == "critical"


def test_helpers_are_safe_without_active_span() -> None:
    # No active span: the span calls must be no-ops rather than raise.
    correlation_id = uuid4()
    rt.bind_correlation_id(correlation_id)
    rt.record_event("noop", detail="x")
    # The log side still binds even without a span.
    assert get_contextvars().get("correlation_id") == str(correlation_id)
