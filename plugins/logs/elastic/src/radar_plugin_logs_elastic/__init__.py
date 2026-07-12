"""RADAR Elasticsearch logs backend plugin.

Portable structural implementation of the ``radar-contracts`` ``LogsBackend``
protocol over the Elasticsearch SDK. Depends only on ``radar-contracts`` and the
``elasticsearch`` client; the consuming application registers the class with its
own plugin registry and constructs it via the plugin-sdk loader.
"""

from __future__ import annotations

from .backend import BACKEND, ElasticLogsBackend

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "ElasticLogsBackend",
    "__version__",
]
