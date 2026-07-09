"""RADAR Gemini LLM provider plugin.

Portable structural implementations of the ``radar-contracts`` provider
protocols over ``google-generativeai``. Depends only on ``radar-contracts``
and the vendor SDK; the consuming application registers the classes with its
own plugin registry. The embedding provider is bound to its own embedding
model string, distinct from any chat model.
"""

from __future__ import annotations

from .provider import PROVIDER, GeminiChatProvider, GeminiEmbeddingProvider

__version__ = "0.4.0"

__all__ = [
    "PROVIDER",
    "GeminiChatProvider",
    "GeminiEmbeddingProvider",
    "__version__",
]
