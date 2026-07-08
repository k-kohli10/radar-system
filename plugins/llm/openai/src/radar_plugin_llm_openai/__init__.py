"""RADAR OpenAI LLM provider plugin.

Portable structural implementations of the ``radar-contracts`` provider
protocols. Depends only on ``radar-contracts`` and the ``openai`` SDK; the
consuming application registers the classes with its own plugin registry.
"""

from __future__ import annotations

from .provider import PROVIDER, OpenAIChatProvider, OpenAIEmbeddingProvider

__version__ = "0.4.0"

__all__ = [
    "PROVIDER",
    "OpenAIChatProvider",
    "OpenAIEmbeddingProvider",
    "__version__",
]
