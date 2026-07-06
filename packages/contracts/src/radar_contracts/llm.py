"""LLM gateway contracts.

Request/response models for the LLM gateway's ``/v1/complete`` and streaming
endpoints, plus the vendor-neutral ``LLMProvider`` Protocol interface
implemented by provider plugins. Embedding and retrieval interfaces live in
``knowledge.py``; this module holds only LLM completion contracts.

This interface is a ``typing.Protocol`` (structural), never an ABC, and is
deliberately free of any vendor SDK types. A concrete adapter for OpenAI,
Anthropic, or Gemini lives in ``plugins/llm/*`` and imports its own SDK there;
nothing in this module does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMMode(StrEnum):
    """Gateway request mode.

    The closed set defined as a Locked Decision: one token maps to one mode.
    """

    FAST = "fast"
    REASON = "reason"
    EXTENDED = "extended"
    EMBED = "embed"


class Message(BaseModel):
    """A single chat message in an LLM request."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(description="Message role, e.g. 'system', 'user', 'assistant'.")
    content: str = Field(description="Message text.")


class Usage(BaseModel):
    """Token accounting for an LLM call.

    ``completion_tokens`` is absent for embedding calls.
    """

    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(ge=0, description="Input tokens counted.")
    completion_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Output tokens produced, if applicable.",
    )


class LLMRequest(BaseModel):
    """A completion request sent to the gateway ``/v1/complete`` endpoint."""

    model_config = ConfigDict(extra="forbid")

    mode: LLMMode = Field(description="Requested gateway mode.")
    messages: list[Message] = Field(description="Ordered chat messages.")
    stream: bool = Field(default=False, description="Whether to stream the response.")


class LLMResponse(BaseModel):
    """A completion response returned by the gateway ``/v1/complete`` endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Response identifier, e.g. 'resp_abc123'.")
    mode: LLMMode = Field(description="Mode that served the request.")
    provider: str = Field(description="Provider that served the request.")
    model: str = Field(description="Concrete model id, e.g. 'gpt-4o'.")
    content: str = Field(description="Generated completion text.")
    usage: Usage = Field(description="Token accounting for the call.")
    latency_ms: int = Field(ge=0, description="End-to-end latency in milliseconds.")


class GatewayStreamEvent(BaseModel):
    """One event in a streamed completion.

    Deltas arrive as ``delta`` text until the terminal event, which sets
    ``done=True`` and may carry final ``usage``.
    """

    model_config = ConfigDict(extra="forbid")

    delta: str = Field(default="", description="Incremental content for this event.")
    done: bool = Field(default=False, description="True on the terminal event.")
    usage: Usage | None = Field(
        default=None,
        description="Token accounting, populated on the terminal event.",
    )


@runtime_checkable
class LLMProvider(Protocol):
    """Interface for a chat/completion provider plugin.

    Implemented by adapters in ``plugins/llm/*``. Implementations map the
    request ``mode`` onto their own concrete model and translate to/from their
    vendor SDK internally; no vendor type appears in this interface.
    """

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Produce a single completion for ``request``."""
        ...

    def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        """Stream a completion for ``request`` as a sequence of events."""
        ...
