"""Metrics backend contract.

``MetricsBackend`` is the vendor-neutral interface for recording the three
metric types RADAR emits across its observability spec: counters, histograms,
and gauges. It is a ``typing.Protocol`` (never an ABC) and references no vendor
type; the Prometheus client lives in ``plugins/metrics/prometheus/`` and imports
its SDK there, not here.

Recording is synchronous: metrics are updated in process and scraped from a
``/metrics`` endpoint, so these methods do not perform I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class MetricsBackend(Protocol):
    """Interface for a metrics recording backend."""

    def increment_counter(
        self,
        name: str,
        *,
        labels: Mapping[str, str] | None = None,
        value: float = 1.0,
    ) -> None:
        """Increment counter ``name`` by ``value`` (default 1.0)."""
        ...

    def observe_histogram(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Record an observation ``value`` into histogram ``name``."""
        ...

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Set gauge ``name`` to ``value``."""
        ...
