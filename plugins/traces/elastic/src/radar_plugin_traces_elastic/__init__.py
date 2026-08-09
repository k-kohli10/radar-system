"""RADAR Elasticsearch traces query backend plugin.

Portable structural implementation of the ``radar-contracts`` ``TraceQuery``
protocol over the Elasticsearch SDK. Depends only on ``radar-contracts`` and the
``elasticsearch`` client; the consuming application registers the class with its
own plugin registry and constructs it via the plugin-sdk loader. This is the read
side of tracing — emission is the telemetry package's OTLP path (ADR 0008).
"""

from __future__ import annotations

from .backend import (
    BACKEND,
    CORRELATION_ID_FIELD,
    TRACES_INDEX,
    ElasticTracesBackend,
)

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "CORRELATION_ID_FIELD",
    "TRACES_INDEX",
    "ElasticTracesBackend",
    "__version__",
]
