"""RADAR telemetry layer.

Observability primitives shared by every RADAR service, kept out of the business
logic so metrics, traces, and events look identical everywhere:

- ``metrics`` — Prometheus metric factories for the platform-wide, LLM, outbox,
  and incident-pipeline metrics, rendered at ``/metrics``.
- ``tracing`` — OpenTelemetry tracer setup exporting via OTLP/gRPC to the OTel
  Collector (forwarded to Elasticsearch, viewed in Kibana APM), plus FastAPI
  request instrumentation.
- ``events`` — helpers to annotate the current span with domain events and to
  thread ``correlation_id``, the join key between structured logs and traces.

See docs/adr/0008-otel-to-elasticsearch.md. Public symbols are re-exported here
so consumers import from the top level::

    from radar_telemetry import setup_tracing, request_metrics
"""

from __future__ import annotations

from .events import bind_correlation_id, record_event
from .metrics import (
    IncidentMetrics,
    LLMMetrics,
    OutboxMetrics,
    PlannerMetrics,
    ReasonerMetrics,
    RequestMetrics,
    create_incident_metrics,
    create_llm_metrics,
    create_outbox_metrics,
    create_planner_metrics,
    create_reasoner_metrics,
    create_request_metrics,
    render_latest,
)
from .tracing import get_tracer, instrument_fastapi, setup_tracing

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # metrics
    "RequestMetrics",
    "LLMMetrics",
    "OutboxMetrics",
    "IncidentMetrics",
    "PlannerMetrics",
    "ReasonerMetrics",
    "create_request_metrics",
    "create_llm_metrics",
    "create_outbox_metrics",
    "create_incident_metrics",
    "create_planner_metrics",
    "create_reasoner_metrics",
    "render_latest",
    # tracing
    "setup_tracing",
    "instrument_fastapi",
    "get_tracer",
    # events
    "bind_correlation_id",
    "record_event",
]
