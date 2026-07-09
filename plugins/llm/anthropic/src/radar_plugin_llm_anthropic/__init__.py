"""RADAR Anthropic LLM provider plugin.

Portable structural implementation of the ``radar-contracts`` ``LLMProvider``
protocol. Depends only on ``radar-contracts`` and the ``anthropic`` SDK; the
consuming application registers the class with its own plugin registry.
Anthropic has no embedding model, so this plugin exposes no
``EmbeddingProvider``.
"""

from __future__ import annotations

from .provider import PROVIDER, AnthropicChatProvider

__version__ = "0.4.0"

__all__ = ["PROVIDER", "AnthropicChatProvider", "__version__"]
