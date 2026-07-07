"""OpenTelemetry tracing setup.

Builds a ``TracerProvider`` that exports spans via OTLP/gRPC to the OTel
Collector (which forwards to Elasticsearch; traces are viewed in Kibana APM), and
instruments FastAPI so every request becomes a span. ``correlation_id`` is set on
spans by :mod:`radar_telemetry.events`, tying logs and traces together in Kibana.

See docs/adr/0008-otel-to-elasticsearch.md.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Tracer


def setup_tracing(
    *,
    service_name: str,
    endpoint: str | None = None,
    resource_attributes: Mapping[str, str] | None = None,
    span_processor: SpanProcessor | None = None,
    set_global: bool = True,
) -> TracerProvider:
    """Build (and by default install) a ``TracerProvider`` for a service.

    Spans export via OTLP/gRPC. ``endpoint`` overrides the collector address
    (default: the ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, else
    ``localhost:4317``). Pass ``span_processor`` to inject a custom processor
    (tests use an in-memory one) and ``set_global=False`` to leave the process
    global provider untouched.
    """
    attributes: dict[str, str] = {SERVICE_NAME: service_name}
    if resource_attributes:
        attributes.update(resource_attributes)

    provider = TracerProvider(resource=Resource.create(attributes))
    if span_processor is None:
        span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    provider.add_span_processor(span_processor)

    if set_global:
        trace.set_tracer_provider(provider)
    return provider


def instrument_fastapi(
    app: FastAPI, *, tracer_provider: TracerProvider | None = None
) -> None:
    """Instrument a FastAPI app so every request produces a span."""
    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)


def get_tracer(name: str) -> Tracer:
    """Return a tracer for the given instrumentation scope (usually ``__name__``)."""
    return trace.get_tracer(name)
