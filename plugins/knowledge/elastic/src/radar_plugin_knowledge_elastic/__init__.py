"""RADAR Elasticsearch knowledge store plugin.

The runbook chunk index over the Elasticsearch SDK: a ``text`` field for BM25 and
a ``dense_vector`` field for kNN, in one mapping, so both halves of hybrid
retrieval run index-side rather than in Python.

The embedding dimension is a required constructor argument: it is fixed into the
``dense_vector`` mapping when the index is created, so moving to an embedding
model with different dimensions means a new index and a full re-index, not a
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
