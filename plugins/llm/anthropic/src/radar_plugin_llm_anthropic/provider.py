"""Anthropic implementation of the RADAR LLM provider contract.

Structural implementation of ``radar_contracts.LLMProvider`` over the
``anthropic`` SDK. Portable by design: depends on ``radar-contracts`` and the
vendor SDK only, never on gateway internals. Vendor exceptions propagate
unwrapped — classification, redaction, timeouts, and retries are the
caller's job. Anthropic has no embedding model, so there is no
``EmbeddingProvider`` here.

Two Anthropic-specific translations from the OpenAI-style message format:

- **System messages** go in the API's single ``system`` string, not the
  messages array. The incoming format allows multiple system messages
  interspersed in the conversation; they are concatenated with newline
  separators (never silently dropped). With no system message, the
  parameter is omitted.
- **``max_tokens`` is a hard API requirement**, not an option. The
  constructor requires a positive ``max_output_tokens`` and fails fast at
  construction (i.e. gateway startup) when it is missing — otherwise every
  request would 400 (non-retryable), which is correct but miserable to
  debug at 3am.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from time import perf_counter

from anthropic import NOT_GIVEN, AsyncAnthropic
from anthropic.types import MessageParam
from radar_contracts import GatewayStreamEvent, LLMRequest, LLMResponse, Usage

PROVIDER = "anthropic"


def _convert(request: LLMRequest) -> tuple[str | None, list[MessageParam]]:
    """Split messages into Anthropic's (system string, conversation) shape."""
    system_parts = [m.content for m in request.messages if m.role == "system"]
    system = "\n".join(system_parts) if system_parts else None
    conversation: list[MessageParam] = [
        {"role": "assistant" if m.role == "assistant" else "user", "content": m.content}
        for m in request.messages
        if m.role != "system"
    ]
    return system, conversation


class AnthropicChatProvider:
    """``LLMProvider`` over Anthropic messages, bound to one model."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        max_output_tokens: int | None = None,
    ) -> None:
        if max_output_tokens is None or max_output_tokens <= 0:
            raise ValueError(
                "Anthropic requires max_tokens on every request: configure a "
                "positive max_output_tokens for any mode routed to the "
                "'anthropic' provider"
            )
        self._client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._model = model
        self._max_output_tokens = max_output_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = perf_counter()
        system, conversation = _convert(request)
        message = await self._client.messages.create(
            model=self._model,
            system=system if system is not None else NOT_GIVEN,
            messages=conversation,
            max_tokens=self._max_output_tokens,
        )
        content = "".join(
            block.text for block in message.content if block.type == "text"
        )
        usage = Usage(
            prompt_tokens=message.usage.input_tokens,
            completion_tokens=message.usage.output_tokens,
        )
        return LLMResponse(
            id=message.id,
            mode=request.mode,
            provider=PROVIDER,
            model=self._model,
            content=content,
            usage=usage,
            latency_ms=int((perf_counter() - started) * 1000),
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        system, conversation = _convert(request)
        async with self._client.messages.stream(
            model=self._model,
            system=system if system is not None else NOT_GIVEN,
            messages=conversation,
            max_tokens=self._max_output_tokens,
        ) as stream:
            async for text in stream.text_stream:
                yield GatewayStreamEvent(delta=text)
            final = await stream.get_final_message()
        yield GatewayStreamEvent(
            done=True,
            usage=Usage(
                prompt_tokens=final.usage.input_tokens,
                completion_tokens=final.usage.output_tokens,
            ),
        )
