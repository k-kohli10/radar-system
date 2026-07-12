"""RADAR Prometheus metrics backend plugin.

Portable structural implementation of the ``radar-contracts`` ``MetricsBackend``
protocol over the ``prometheus_client`` SDK. Depends only on ``radar-contracts``
and the ``prometheus_client`` library; the consuming application registers the
class with its own plugin registry and constructs it via the plugin-sdk loader.
"""

from __future__ import annotations

from .backend import BACKEND, PrometheusMetricsBackend

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "PrometheusMetricsBackend",
    "__version__",
]
