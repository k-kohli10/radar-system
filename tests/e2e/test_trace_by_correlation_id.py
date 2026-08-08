"""Done-condition: a mock alert's trace is retrievable from Elasticsearch by
correlation_id ALONE (ADR 0008 — the log/trace join key, viewed in Kibana APM).

It emits one span per pipeline hop (ingestion → watcher → planner → reasoner →
feedback), each with its own service.name and ALL sharing one correlation_id,
through the REAL telemetry emit path: `setup_tracing` → OTLP → the running
collector → Elasticsearch, with `radar_telemetry.bind_correlation_id` (the
span-stamping binder step 3 wired) putting correlation_id on each span. Then it
retrieves the whole trace with `ElasticTracesBackend.get_trace(correlation_id)` —
the plugin takes ONLY the id — and asserts every hop comes back.

FAIL-LOUD, NOT SKIP. The dependency is the trace backend: the OTLP collector and
Elasticsearch (with the traces data-stream template). If any is absent this test
FAILS — it does not skip. A green run on a stack that is not fully up is a false
green (the Phase 9 "293 skipped" lesson), and this is a milestone done-condition.
It is `infra` (so `make test` / CI run it, `make test-quick` skips it) but is not
self-orchestrating: bring the observability stack up first (`make dev`).
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from typing import Any

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from radar_plugin_traces_elastic import ElasticTracesBackend
from radar_telemetry import bind_correlation_id, setup_tracing

pytestmark = pytest.mark.infra

ES = "http://localhost:9200"
OTLP_HOST, OTLP_PORT = "localhost", 4317
HOPS = [
    "ingestion",
    "watcher-agent",
    "planner-agent",
    "reasoner-agent",
    "feedback-service",
]


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _http_ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(autouse=True)
def _require_trace_backend() -> None:
    missing = []
    if not _tcp_open(OTLP_HOST, OTLP_PORT):
        missing.append("OTLP collector :4317")
    if not _http_ok(f"{ES}/_cluster/health"):
        missing.append("Elasticsearch :9200")
    elif not _http_ok(f"{ES}/_index_template/radar-traces"):
        missing.append("traces data-stream template (es-traces-init)")
    if missing:
        pytest.fail(
            "trace backend not up (" + ", ".join(missing) + "); a done-condition "
            "proof must FAIL, not skip — bring the stack up with `make dev`"
        )


def _emit_pipeline_trace(correlation_id: str) -> None:
    """Emit one span per hop, distinct service.name, shared correlation_id."""
    providers: list[TracerProvider] = []
    for hop in HOPS:
        exporter = OTLPSpanExporter(endpoint=f"{OTLP_HOST}:{OTLP_PORT}", insecure=True)
        provider = setup_tracing(
            service_name=hop,
            span_processor=BatchSpanProcessor(exporter),
            set_global=False,
        )
        providers.append(provider)
        with provider.get_tracer("step10").start_as_current_span(f"{hop} handle event"):
            # The real binder: stamps correlation_id onto the current span.
            bind_correlation_id(correlation_id)
    for provider in providers:
        assert provider.force_flush(), "span export flush failed"
        provider.shutdown()  # type: ignore[no-untyped-call]  # OTel SDK is untyped here


async def _poll_trace(
    correlation_id: str, *, expect: int, timeout: float = 45.0
) -> list[dict[str, Any]]:
    backend = ElasticTracesBackend(hosts=ES)
    spans: list[dict[str, Any]] = []
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                spans = await backend.get_trace(correlation_id)
            except Exception:
                spans = []  # data stream not created until the first span lands
            if len(spans) >= expect:
                return spans
            await asyncio.sleep(2)
        return spans
    finally:
        await backend.close()


async def test_mock_alert_traceable_by_correlation_id_alone() -> None:
    correlation_id = "step10-" + uuid.uuid4().hex
    _emit_pipeline_trace(correlation_id)

    spans = await _poll_trace(correlation_id, expect=len(HOPS))

    # Retrieved by correlation_id ALONE (get_trace takes only the id).
    assert len(spans) >= len(HOPS), f"got {len(spans)} spans for {correlation_id}"
    # End to end: every hop is present in the reconstructed trace.
    assert {s.get("name") for s in spans} == {f"{h} handle event" for h in HOPS}
    # Every returned span really carries this correlation_id.
    for span in spans:
        got = span.get("attributes", {}).get("correlation_id")
        assert got == correlation_id, f"span {span.get('name')!r} has {got!r}"


async def test_unrelated_correlation_id_returns_nothing() -> None:
    # Teeth: the query is correlation-id-specific, not "return everything".
    backend = ElasticTracesBackend(hosts=ES)
    try:
        spans = await backend.get_trace("step10-absent-" + uuid.uuid4().hex)
    finally:
        await backend.close()
    assert spans == []
