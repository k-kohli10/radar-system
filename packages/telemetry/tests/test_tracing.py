"""OpenTelemetry tracing setup and instrumentation tests."""

from __future__ import annotations

import radar_telemetry as rt
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

Tracing = tuple[TracerProvider, InMemorySpanExporter]


def test_setup_tracing_stamps_service_name(tracing: Tracing) -> None:
    provider, exporter = tracing
    with provider.get_tracer("t").start_as_current_span("op"):
        pass
    resource_attrs = exporter.get_finished_spans()[0].resource.attributes
    assert resource_attrs["service.name"] == "test-service"


def test_setup_tracing_does_not_touch_global_provider(tracing: Tracing) -> None:
    provider, _ = tracing
    assert trace.get_tracer_provider() is not provider


def test_instrument_fastapi_records_request_span(tracing: Tracing) -> None:
    provider, exporter = tracing
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    rt.instrument_fastapi(app, tracer_provider=provider)
    assert TestClient(app).get("/healthz").status_code == 200

    server_spans = [
        span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    attributes = server_spans[0].attributes
    assert attributes is not None
    # The request span carries both the matched route and the HTTP status.
    assert attributes["http.route"] == "/healthz"
    assert attributes["http.status_code"] == 200
