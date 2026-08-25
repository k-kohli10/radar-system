"""Streaming support: retry/fallback at stream start, SSE encoding, and the
terminal error event for mid-stream failures.

A stream splits the failure policy in two at the first event:

- **Before the first event** nothing has been sent, so the full retry and
  fallback policy applies. :func:`prime_stream` starts the provider stream
  under ``run_with_fallback`` and returns only once the first event has
  arrived — or raises ``AllProvidersFailedError`` so the API layer can still
  answer a clean 503 instead of a broken SSE body.
- **After the first event** bytes are on the wire: the gateway cannot fall
  back, and it must never just close the connection — a silently truncated
  stream is indistinguishable from a complete one. On mid-stream failure
  :func:`sse_stream` emits a terminal error event before closing::

      data: {"error": "stream_failed", "provider": "openai", "recoverable": false}

  No content, ever: provider name and a ``recoverable`` flag only (mapped
  from the failure's retryability), so the client can decide to retry the
  whole request against the gateway or degrade.

Normal events are encoded as one SSE ``data:`` line per
:class:`GatewayStreamEvent`; the contract's ``done`` flag is the end-of-stream
sentinel, after which the connection closes normally.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from radar_common import get_logger
from radar_contracts import GatewayStreamEvent, LLMMode, LLMRequest

from radar_llm_gateway.core.errors import ProviderError
from radar_llm_gateway.gateway.circuit_breaker import CircuitBreaker
from radar_llm_gateway.gateway.fallback import run_with_fallback
from radar_llm_gateway.gateway.model_router import ModelRouter
from radar_llm_gateway.gateway.retry import RETRY_DELAYS_SECONDS
from radar_llm_gateway.providers.base import ProviderBinding

SSE_MEDIA_TYPE = "text/event-stream"

_log = get_logger(__name__)


@dataclass
class PrimedStream:
    """A provider stream that has already produced its first event."""

    binding: ProviderBinding
    first_event: GatewayStreamEvent
    events: AsyncIterator[GatewayStreamEvent]


async def prime_stream(
    mode: LLMMode,
    router: ModelRouter,
    request: LLMRequest,
    *,
    delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_fallback: Callable[[ProviderBinding, ProviderBinding], None] | None = None,
    on_error: Callable[[ProviderError], None] | None = None,
    breaker_for: Callable[[ProviderBinding], CircuitBreaker] | None = None,
) -> PrimedStream:
    """Start streaming under the full retry/fallback policy.

    Returns once the first event arrives; failures up to that point are
    retried and failed over exactly like a completion, and exhaustion raises
    ``AllProvidersFailedError`` (the caller has not sent anything yet). The
    circuit breaker gates stream priming exactly as it gates a completion.
    """

    async def start(binding: ProviderBinding) -> PrimedStream:
        events = aiter(binding.stream(request))
        try:
            first_event = await anext(events)
        except StopAsyncIteration:
            # A stream that ends before its first event is a provider fault;
            # classify retryable so the normal policy applies.
            raise ProviderError(
                binding.provider_name,
                binding.model,
                retryable=True,
                reason="EmptyStream",
            ) from None
        return PrimedStream(binding=binding, first_event=first_event, events=events)

    return await run_with_fallback(
        mode,
        router,
        start,
        delays=delays,
        sleep=sleep,
        on_fallback=on_fallback,
        on_error=on_error,
        breaker_for=breaker_for,
    )


def encode_sse(event: GatewayStreamEvent) -> str:
    """Encode one stream event as an SSE ``data:`` frame."""
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def encode_sse_error(provider: str, *, recoverable: bool) -> str:
    """The terminal error frame: provider and recoverable flag only."""
    payload = json.dumps(
        {"error": "stream_failed", "provider": provider, "recoverable": recoverable}
    )
    return f"data: {payload}\n\n"


async def sse_stream(primed: PrimedStream) -> AsyncIterator[str]:
    """Yield SSE frames for a primed stream; never close silently on failure.

    A mid-stream :class:`ProviderError` (already redacted to class name +
    status) becomes the terminal error event with ``recoverable`` taken from
    its retryability; an unexpected exception is a gateway bug and becomes a
    non-recoverable error event, logged by class name only.
    """
    yield encode_sse(primed.first_event)
    try:
        async for event in primed.events:
            yield encode_sse(event)
    except ProviderError as exc:
        _log.warning(
            "llm.stream_failed",
            provider=exc.provider,
            model=exc.model,
            status_code=exc.status_code,
            reason=exc.reason,
            recoverable=exc.retryable,
        )
        yield encode_sse_error(exc.provider, recoverable=exc.retryable)
    except Exception as exc:
        _log.error(
            "llm.stream_failed",
            provider=primed.binding.provider_name,
            model=primed.binding.model,
            error_type=type(exc).__name__,
            recoverable=False,
        )
        yield encode_sse_error(primed.binding.provider_name, recoverable=False)
