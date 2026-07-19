"""RADAR Elasticsearch knowledge store plugin.

Portable implementation of the runbook chunk index over the Elasticsearch SDK:
a ``text`` field for BM25 and a ``dense_vector`` field for kNN, in one mapping,
so both halves of hybrid retrieval run index-side rather than in Python.

Depends only on ``radar-contracts`` and the ``elasticsearch`` client. The
consuming application registers this class with its own plugin registry and
constructs it from config.

The embedding dimension is a required constructor argument: it is fixed into the
``dense_vector`` mapping when the index is created, so changing embedding model
to one with different dimensions means a new index and a full re-index, not a
config flip.
"""

from __future__ import annotations

from .store import BACKEND, ElasticKnowledgeStore, build_mapping

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "ElasticKnowledgeStore",
    "build_mapping",
    "__version__",
]
