"""Prometheus implementation of the RADAR metrics backend contract.

Structural implementation of ``radar_contracts.MetricsBackend`` over
``prometheus_client``.

Recording is in-process and synchronous: counters, histograms, and gauges are
updated in a ``CollectorRegistry`` and scraped from the service's ``/metrics``
endpoint, and no I/O happens here. ``prometheus_client`` requires each metric to
be created once with a fixed set of label names, so this backend lazily creates a
metric on first use (taking its label names from that first call) and caches it,
reusing the same object on later calls. A name reused with a different metric
type collides in the registry and raises, surfacing the misuse.

POC scope: default histogram buckets, no metric-name namespacing or per-metric
bucket tuning (deferred to Phase 13).
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

BACKEND = "prometheus"
"""Registry name this backend registers under for ``MetricsBackend``."""


class PrometheusMetricsBackend:
    """``MetricsBackend`` recording into a Prometheus ``CollectorRegistry``."""

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        """Record into ``registry``, defaulting to the process-global registry.

        The default is ``prometheus_client.REGISTRY``, the one
        ``generate_latest()`` scrapes for a standard ``/metrics`` endpoint, so the
        backend works with no configuration. Tests pass a fresh registry to stay
        isolated.
        """
        self._registry = registry if registry is not None else REGISTRY
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}

    def increment_counter(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
        value: float = 1.0,
    ) -> None:
        """Increment counter ``name`` by ``value`` (default 1.0)."""
        counter = self._counters.get(name)
        if counter is None:
            counter = Counter(name, name, _label_names(labels), registry=self._registry)
            self._counters[name] = counter
        _with_labels(counter, labels).inc(value)

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Record an observation ``value`` into histogram ``name``."""
        histogram = self._histograms.get(name)
        if histogram is None:
            histogram = Histogram(
                name, name, _label_names(labels), registry=self._registry
            )
            self._histograms[name] = histogram
        _with_labels(histogram, labels).observe(value)

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Set gauge ``name`` to ``value``."""
        gauge = self._gauges.get(name)
        if gauge is None:
            gauge = Gauge(name, name, _label_names(labels), registry=self._registry)
            self._gauges[name] = gauge
        _with_labels(gauge, labels).set(value)


def _label_names(labels: Mapping[str, str] | None) -> tuple[str, ...]:
    """The metric's fixed label names, taken from the first call's label keys."""
    return tuple(sorted(labels)) if labels else ()


def _with_labels[M: (Counter, Histogram, Gauge)](
    metric: M, labels: Mapping[str, str] | None
) -> M:
    """Return the per-label-set child, or the metric itself when it has no labels.

    Generic in the metric type so the concrete ``Counter``/``Histogram``/
    ``Gauge`` flows through to the caller's ``inc``/``observe``/``set``.
    """
    return metric.labels(**labels) if labels else metric
