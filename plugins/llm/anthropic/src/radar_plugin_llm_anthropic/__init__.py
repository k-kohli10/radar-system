"""RADAR Anthropic LLM provider plugin.

Structural implementation of the ``radar-contracts`` ``LLMProvider`` protocol over
the ``anthropic`` SDK. Anthropic has no embedding model, so this plugin exposes no
``EmbeddingProvider``.
"""

from __future__ import annotations

from .provider import PROVIDER, AnthropicChatProvider

__version__ = "0.4.0"

__all__ = ["PROVIDER", "AnthropicChatProvider", "__version__"]
