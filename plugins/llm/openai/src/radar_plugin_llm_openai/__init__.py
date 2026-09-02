"""RADAR OpenAI LLM provider plugin.

Structural implementations of the ``radar-contracts`` provider protocols over the
``openai`` SDK.
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
