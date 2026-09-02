"""OpenAI implementations of the RADAR LLM provider contracts.

Structural implementations of ``radar_contracts.LLMProvider`` and
``radar_contracts.EmbeddingProvider`` over the ``openai`` SDK. Vendor exceptions
propagate unwrapped: classification, redaction, timeouts, and retries are the
caller's job (in RADAR, the llm-gateway's provider layer).

Each instance is bound to one concrete model at construction; the gateway builds
one instance per configured mode. SDK-internal retries are disabled
(``max_retries=0``) so the caller's retry policy is the only one in play.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import perf_counter
from typing import cast

from openai import NOT_GIVEN, AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from radar_contracts import GatewayStreamEvent, LLMRequest, LLMResponse, Usage

PROVIDER = "openai"


def _messages(request: LLMRequest) -> list[ChatCompletionMessageParam]:
    return [
        cast(
            "ChatCompletionMessageParam",
            {"role": message.role, "content": message.content},
        )
        for message in request.messages
    ]


class OpenAIChatProvider:
    """``LLMProvider`` over OpenAI chat completions, bound to one model."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        max_output_tokens: int | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0)
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=_messages(request),
            max_completion_tokens=(
                self._max_output_tokens
                if self._max_output_tokens is not None
                else NOT_GIVEN
            ),
        )
        content = completion.choices[0].message.content or ""
        usage = Usage(
            prompt_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            completion_tokens=(
                completion.usage.completion_tokens if completion.usage else 0
            ),
        )
        return LLMResponse(
            id=completion.id,
            mode=request.mode,
            provider=PROVIDER,
            model=self._model,
            content=content,
            usage=usage,
            latency_ms=int((perf_counter() - started) * 1000),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=_messages(request),
            max_completion_tokens=(
                self._max_output_tokens
                if self._max_output_tokens is not None
                else NOT_GIVEN
            ),
            stream=True,
            stream_options={"include_usage": True},
        )
        usage: Usage | None = None
        async for chunk in stream:
            # With include_usage the final chunk carries usage and no choices.
            if chunk.usage is not None:
                usage = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                )
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield GatewayStreamEvent(delta=delta)
        yield GatewayStreamEvent(done=True, usage=usage)


class OpenAIEmbeddingProvider:
    """``EmbeddingProvider`` over OpenAI embeddings, bound to one model."""

    def __init__(self, *, model: str, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0)
        self._model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(model=self._model, input=texts)
        # The API returns one datum per input with an explicit index; sort to
        # guarantee order matches the inputs.
        ordered = sorted(response.data, key=lambda datum: datum.index)
        return [datum.embedding for datum in ordered]
