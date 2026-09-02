"""Gemini implementations of the RADAR LLM provider contracts.

Structural implementations of ``radar_contracts.LLMProvider`` and
``radar_contracts.EmbeddingProvider`` over the ``google-generativeai`` SDK.
Vendor exceptions propagate unwrapped: classification, redaction, timeouts, and
retries are the caller's job.

Gemini-specific translations:

- **System messages** map to the model's ``system_instruction`` (multiple
  system messages are newline-joined, matching the Anthropic plugin);
  assistant turns map to role ``model``.
- **The embedding model is a separate model string** from the chat model
  (e.g. ``models/text-embedding-004`` vs ``gemini-1.5-pro``):
  :class:`GeminiEmbeddingProvider` takes its own ``model`` at construction.
- Gemini responses carry no id; one is generated per response.

SDK caveats: ``genai.configure`` sets the API key *process-globally*, so all
Gemini instances in one process share the last-configured key (fine for RADAR:
one ``gemini_api_key`` secret). The SDK's async methods
(``generate_content_async``, ``embed_content_async``) ride on ``grpc.aio``; see
the gateway phase notes on Python 3.14 async status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any
from uuid import uuid4

import google.generativeai as genai
from radar_contracts import GatewayStreamEvent, LLMRequest, LLMResponse, Usage

PROVIDER = "gemini"


def _convert(request: LLMRequest) -> tuple[str | None, list[dict[str, Any]]]:
    """Split messages into Gemini's (system_instruction, contents) shape."""
    system_parts = [m.content for m in request.messages if m.role == "system"]
    system = "\n".join(system_parts) if system_parts else None
    contents = [
        {
            "role": "model" if m.role == "assistant" else "user",
            "parts": [m.content],
        }
        for m in request.messages
        if m.role != "system"
    ]
    return system, contents


def _usage(response: Any) -> Usage:
    metadata = getattr(response, "usage_metadata", None)
    return Usage(
        prompt_tokens=getattr(metadata, "prompt_token_count", 0) or 0,
        completion_tokens=getattr(metadata, "candidates_token_count", 0) or 0,
    )


def _text(candidate_holder: Any) -> str:
    parts: list[str] = []
    for candidate in getattr(candidate_holder, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            text = getattr(part, "text", "")
            if text:
                parts.append(text)
    return "".join(parts)


class GeminiChatProvider:
    """``LLMProvider`` over Gemini generate_content, bound to one model."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        max_output_tokens: int | None = None,
    ) -> None:
        genai.configure(api_key=api_key)  # process-global; see module docstring
        self._model_name = model
        self._max_output_tokens = max_output_tokens

    def _generation_config(self) -> dict[str, int] | None:
        if self._max_output_tokens is None:
            return None
        return {"max_output_tokens": self._max_output_tokens}

    def _model_for(self, system: str | None) -> Any:
        # system_instruction is fixed per GenerativeModel instance, and it
        # varies per request, so the (lightweight) model object is built per
        # call rather than at construction.
        return genai.GenerativeModel(
            model_name=self._model_name, system_instruction=system
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        system, contents = _convert(request)
        response = await self._model_for(system).generate_content_async(
            contents, generation_config=self._generation_config()
        )
        return LLMResponse(
            id=f"gemini-{uuid4().hex}",
            mode=request.mode,
            provider=PROVIDER,
            model=self._model_name,
            content=_text(response),
            usage=_usage(response),
            latency_ms=int((perf_counter() - started) * 1000),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        system, contents = _convert(request)
        response = await self._model_for(system).generate_content_async(
            contents, generation_config=self._generation_config(), stream=True
        )
        async for chunk in response:
            delta = _text(chunk)
            if delta:
                yield GatewayStreamEvent(delta=delta)
        yield GatewayStreamEvent(done=True, usage=_usage(response))


class GeminiEmbeddingProvider:
    """``EmbeddingProvider`` over Gemini embed_content.

    Bound to its *own* embedding model string (e.g. ``models/text-embedding-004``);
    the Gemini embedding API takes different models than chat, and the gateway
    passes the embed mode's configured model here.
    """

    def __init__(self, *, model: str, api_key: str) -> None:
        genai.configure(api_key=api_key)  # process-global; see module docstring
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        result = await genai.embed_content_async(model=self._model, content=texts)
        embeddings = result["embedding"]
        return [[float(value) for value in vector] for vector in embeddings]
