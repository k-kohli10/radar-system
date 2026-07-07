"""Shared fixtures for the radar_telemetry test suite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from radar_telemetry import setup_tracing
from structlog.contextvars import clear_contextvars


@pytest.fixture(autouse=True)
def _clear_log_context() -> Iterator[None]:
    clear_contextvars()
    yield
    clear_contextvars()


@pytest.fixture
def tracing() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    """A non-global TracerProvider exporting spans to memory for assertions."""
    exporter = InMemorySpanExporter()
    provider = setup_tracing(
        service_name="test-service",
        span_processor=SimpleSpanProcessor(exporter),
        set_global=False,
    )
    yield provider, exporter
