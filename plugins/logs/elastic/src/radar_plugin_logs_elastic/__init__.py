"""RADAR Elasticsearch logs backend plugin.

Structural implementation of the ``radar-contracts`` ``LogsBackend`` protocol over
the Elasticsearch SDK.
"""

from __future__ import annotations

from .backend import BACKEND, ElasticLogsBackend

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "ElasticLogsBackend",
    "__version__",
]
