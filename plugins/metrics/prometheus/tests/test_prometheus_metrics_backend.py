"""Behavioral tests for the Prometheus metrics backend (isolated registry).

Conformance (``issubclass``) only proves ``PrometheusMetricsBackend`` is *shaped*
like ``MetricsBackend``. These prove it *behaves*: counters increment (by 1.0 by
default and by an explicit value) and accumulate across calls onto the same
series, histograms record count and sum, gauges take absolute values up and
down, and reusing a metric name does not re-register (while reusing it as a
different metric type surfaces the registry collision). Each test uses a fresh
``CollectorRegistry`` so there is no global state between tests.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry
from radar_plugin_metrics_prometheus import PrometheusMetricsBackend


@pytest.fixture
def registry() -> CollectorRegistry:
    return CollectorRegistry()


@pytest.fixture
def backend(registry: CollectorRegistry) -> PrometheusMetricsBackend:
    return PrometheusMetricsBackend(registry=registry)


def test_increment_counter_defaults_to_one_and_accumulates(
    backend: PrometheusMetricsBackend, registry: CollectorRegistry
) -> None:
    labels = {"service": "order-service"}
    backend.increment_counter("radar_requests_total", labels=labels)
    backend.increment_counter("radar_requests_total", labels=labels, value=3.0)

    # Same name + labels resolves to one series that accumulates to 1.0 + 3.0.
    assert registry.get_sample_value("radar_requests_total", labels) == 4.0


def test_counter_series_are_kept_separate_by_labels(
    backend: PrometheusMetricsBackend, registry: CollectorRegistry
) -> None:
    backend.increment_counter("radar_errors_total", labels={"error_type": "timeout"})
    backend.increment_counter("radar_errors_total", labels={"error_type": "timeout"})
    backend.increment_counter("radar_errors_total", labels={"error_type": "conflict"})

    assert (
        registry.get_sample_value("radar_errors_total", {"error_type": "timeout"})
        == 2.0
    )
    assert (
        registry.get_sample_value("radar_errors_total", {"error_type": "conflict"})
        == 1.0
    )


def test_counter_without_labels(
    backend: PrometheusMetricsBackend, registry: CollectorRegistry
) -> None:
    backend.increment_counter("radar_events_total")
    assert registry.get_sample_value("radar_events_total", {}) == 1.0


def test_observe_histogram_records_count_and_sum(
    backend: PrometheusMetricsBackend, registry: CollectorRegistry
) -> None:
    labels = {"endpoint": "/alerts"}
    backend.observe_histogram("radar_request_duration_seconds", 0.2, labels=labels)
    backend.observe_histogram("radar_request_duration_seconds", 0.5, labels=labels)

    count = registry.get_sample_value("radar_request_duration_seconds_count", labels)
    total = registry.get_sample_value("radar_request_duration_seconds_sum", labels)
    assert count == 2.0
    assert total == pytest.approx(0.7)


def test_set_gauge_takes_absolute_value_up_and_down(
    backend: PrometheusMetricsBackend, registry: CollectorRegistry
) -> None:
    backend.set_gauge("radar_outbox_depth", 5.0)
    assert registry.get_sample_value("radar_outbox_depth", {}) == 5.0
    backend.set_gauge("radar_outbox_depth", 2.0)
    assert registry.get_sample_value("radar_outbox_depth", {}) == 2.0


def test_reusing_a_metric_name_does_not_re_register(
    backend: PrometheusMetricsBackend,
) -> None:
    # A second call for the same counter must reuse the cached object, not try
    # to register a duplicate (which prometheus_client rejects).
    backend.increment_counter("radar_incidents_total", labels={"severity": "high"})
    backend.increment_counter("radar_incidents_total", labels={"severity": "high"})


def test_same_name_as_a_different_type_surfaces_the_collision(
    backend: PrometheusMetricsBackend,
) -> None:
    backend.increment_counter("radar_conflict")
    # Reusing the name as a gauge collides in the registry; the error is raised,
    # not swallowed.
    with pytest.raises(ValueError):
        backend.set_gauge("radar_conflict", 1.0)
