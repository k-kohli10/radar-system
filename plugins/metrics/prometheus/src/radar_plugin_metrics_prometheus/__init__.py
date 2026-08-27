"""RADAR Prometheus metrics backend plugin.

Structural implementation of the ``radar-contracts`` ``MetricsBackend`` protocol
over the ``prometheus_client`` SDK.
"""

from __future__ import annotations

from .backend import BACKEND, PrometheusMetricsBackend

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "PrometheusMetricsBackend",
    "__version__",
]
