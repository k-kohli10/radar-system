"""Prometheus metric factory tests."""

from __future__ import annotations

import radar_telemetry as rt
from prometheus_client import CollectorRegistry

EXPECTED_METRICS = {
    # platform-wide
    "radar_requests_total",
    "radar_request_duration_seconds",
    "radar_errors_total",
    # llm
    "radar_llm_requests_total",
    "radar_llm_duration_seconds",
    "radar_llm_time_to_first_token_seconds",
    "radar_llm_tokens_total",
    "radar_llm_provider_errors_total",
    "radar_llm_fallback_total",
    "radar_llm_template_fallback_total",
    # outbox
    "radar_outbox_depth",
    "radar_outbox_processing",
    "radar_outbox_dead_letter_total",
    "radar_outbox_dispatch_duration_seconds",
    "radar_outbox_retry_total",
    # incident pipeline
    "radar_incidents_total",
    "radar_incident_duration_seconds",
    "radar_recommendations_total",
    "radar_recommendations_fallback_total",
    "radar_feedback_total",
}


def _type_names(registry: CollectorRegistry) -> set[str]:
    payload, _ = rt.render_latest(registry)
    return {
        line.split(" ")[2]
        for line in payload.decode().splitlines()
        if line.startswith("# TYPE")
    }


def test_all_metric_families_register_expected_names() -> None:
    registry = CollectorRegistry()
    rt.create_request_metrics(registry)
    rt.create_llm_metrics(registry)
    rt.create_outbox_metrics(registry)
    rt.create_incident_metrics(registry)
    assert EXPECTED_METRICS <= _type_names(registry)


def test_render_latest_returns_prometheus_text() -> None:
    registry = CollectorRegistry()
    rt.create_request_metrics(registry)
    payload, content_type = rt.render_latest(registry)
    assert isinstance(payload, bytes)
    assert "text/plain" in content_type


def test_counter_labels_increment() -> None:
    registry = CollectorRegistry()
    metrics = rt.create_request_metrics(registry)
    labels = {"service": "watcher-agent", "endpoint": "/events", "status_code": "200"}
    metrics.requests_total.labels(**labels).inc()
    metrics.requests_total.labels(**labels).inc()
    assert registry.get_sample_value("radar_requests_total", labels) == 2.0


def test_histogram_observations() -> None:
    registry = CollectorRegistry()
    metrics = rt.create_request_metrics(registry)
    labels = {"service": "watcher-agent", "endpoint": "/events"}
    metrics.request_duration_seconds.labels(**labels).observe(0.10)
    metrics.request_duration_seconds.labels(**labels).observe(0.30)
    count = registry.get_sample_value("radar_request_duration_seconds_count", labels)
    total = registry.get_sample_value("radar_request_duration_seconds_sum", labels)
    assert count == 2.0
    assert total is not None
    assert abs(total - 0.40) < 1e-9


def test_gauge_set() -> None:
    registry = CollectorRegistry()
    metrics = rt.create_outbox_metrics(registry)
    metrics.depth.set(7)
    assert registry.get_sample_value("radar_outbox_depth") == 7.0


def test_separate_registries_avoid_duplicate_registration() -> None:
    # Building the same family on two registries must not raise.
    rt.create_request_metrics(CollectorRegistry())
    rt.create_request_metrics(CollectorRegistry())
