"""RADAR Elasticsearch traces query backend plugin.

Structural implementation of the ``radar-contracts`` ``TraceQuery`` protocol over
the Elasticsearch SDK. This is the read side of tracing; emission is the telemetry
package's OTLP path (ADR 0008).
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
