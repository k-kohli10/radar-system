"""The gateway orchestration service.

:class:`GatewayService` is what the API routes call after security has run
(steps 1-5 of the validation order live in ``core.security`` and the routes);
it executes steps 7-11 — route to provider, per-mode timeout, retry, fallback
— and owns the request-level observability:

- ``radar_llm_requests_total{mode, provider, status}`` and
  ``radar_llm_duration_seconds{mode, provider}`` per request;
- ``radar_llm_tokens_total{mode, provider, direction}`` from usage;
- ``radar_llm_provider_errors_total{mode, provider, error}`` once per failing
  provider call (every retry attempt counts);
- ``radar_llm_fallback_total{from_provider, to_provider}`` on failover;
- ``radar_llm_time_to_first_token_seconds{mode, provider}`` for streams;
- one ``llm.request`` log line per request with exactly the allowed fields:
  mode, provider, model, prompt_tokens, completion_tokens, latency_ms,
  status_code. Message content, tokens, keys, and raw responses never appear.

``latency_ms`` on the returned response is gateway-measured wall time
including retries and fallback — what the caller actually experienced — and
overrides the plugin's own call-local measurement.

Embeddings: the ``EmbeddingProvider`` contract returns only vectors, so the
``usage.prompt_tokens`` reported for ``/v1/embed`` is the gateway's own
estimate (the same ~4 chars/token count already used for admission).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter

from radar_common import get_logger
from radar_contracts import GatewayStreamEvent, LLMMode, LLMRequest, LLMResponse, Usage
from radar_telemetry import LLMMetrics

from radar_llm_gateway.core.config import GatewayConfig, ModeConfig
from radar_llm_gateway.core.errors import (
    AllProvidersFailedError,
    ProviderError,
    ProviderTimeoutError,
)
from radar_llm_gateway.gateway.fallback import run_with_fallback
from radar_llm_gateway.gateway.model_router import ModelRouter
from radar_llm_gateway.gateway.retry import RETRY_DELAYS_SECONDS
from radar_llm_gateway.gateway.stream import PrimedStream, prime_stream, sse_stream
from radar_llm_gateway.providers.base import ProviderBinding

_log = get_logger(__name__)


@dataclass(frozen=True)
class EmbedResult:
    """What ``/v1/embed`` needs to build its response."""

    embeddings: list[list[float]]
    provider: str
    model: str
    prompt_tokens: int
    latency_ms: int


def _error_label(exc: ProviderError) -> str:
    """A bounded-cardinality value for the ``error`` metric label."""
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if exc.reason:
        return exc.reason  # vendor exception class name, a bounded set
    if exc.status_code is not None:
        return f"status_{exc.status_code}"
    return "unknown"


class GatewayService:
    """Executes validated gateway requests against the routed providers."""

    def __init__(
        self,
        *,
        config: GatewayConfig,
        router: ModelRouter,
        metrics: LLMMetrics,
        delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._router = router
        self._metrics = metrics
        self._delays = delays
        self._sleep = sleep

    def mode_config(self, mode: LLMMode) -> ModeConfig:
        """The limits for ``mode``, for the routes' budget enforcement."""
        return self._config.modes[mode]

    # ---------------------------------------------------------------- hooks

    def _on_error(self, mode: LLMMode) -> Callable[[ProviderError], None]:
        def hook(exc: ProviderError) -> None:
            self._metrics.provider_errors_total.labels(
                mode.value, exc.provider, _error_label(exc)
            ).inc()

        return hook

    def _on_fallback(self, primary: ProviderBinding, fb: ProviderBinding) -> None:
        self._metrics.fallback_total.labels(
            primary.provider_name, fb.provider_name
        ).inc()

    def _record_request(
        self,
        mode: LLMMode,
        provider: str,
        model: str,
        *,
        status: str,
        status_code: int,
        elapsed_seconds: float,
        usage: Usage | None,
        stream: bool = False,
    ) -> None:
        self._metrics.requests_total.labels(mode.value, provider, status).inc()
        self._metrics.duration_seconds.labels(mode.value, provider).observe(
            elapsed_seconds
        )
        if usage is not None:
            self._metrics.tokens_total.labels(mode.value, provider, "prompt").inc(
                usage.prompt_tokens
            )
            if usage.completion_tokens is not None:
                self._metrics.tokens_total.labels(
                    mode.value, provider, "completion"
                ).inc(usage.completion_tokens)
        _log.info(
            "llm.request",
            mode=mode.value,
            provider=provider,
            model=model,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            latency_ms=int(elapsed_seconds * 1000),
            status_code=status_code,
            stream=stream,
        )

    def _record_total_failure(
        self, mode: LLMMode, started: float, *, stream: bool
    ) -> None:
        primary = self._router.primary_for(mode)
        self._record_request(
            mode,
            primary.provider_name,
            primary.model,
            status="error",
            status_code=503,
            elapsed_seconds=perf_counter() - started,
            usage=None,
            stream=stream,
        )

    # ------------------------------------------------------------- complete

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Step 7-11 for a non-streaming completion."""
        started = perf_counter()
        mode = request.mode
        try:
            response = await run_with_fallback(
                mode,
                self._router,
                lambda binding: binding.complete(request),
                delays=self._delays,
                sleep=self._sleep,
                on_fallback=self._on_fallback,
                on_error=self._on_error(mode),
            )
        except AllProvidersFailedError:
            self._record_total_failure(mode, started, stream=False)
            raise
        elapsed = perf_counter() - started
        response = response.model_copy(
            update={"latency_ms": int(elapsed * 1000), "mode": mode}
        )
        self._record_request(
            mode,
            response.provider,
            response.model,
            status="success",
            status_code=200,
            elapsed_seconds=elapsed,
            usage=response.usage,
        )
        return response

    # --------------------------------------------------------------- stream

    async def stream_sse(self, request: LLMRequest) -> AsyncIterator[str]:
        """Step 7-11 for a streaming completion; returns the SSE frames.

        Priming happens *before* this returns, so total provider failure
        raises ``AllProvidersFailedError`` here and the route still answers a
        clean 503 — no SSE bytes have been sent yet.
        """
        started = perf_counter()
        mode = request.mode
        try:
            primed = await prime_stream(
                mode,
                self._router,
                request,
                delays=self._delays,
                sleep=self._sleep,
                on_fallback=self._on_fallback,
                on_error=self._on_error(mode),
            )
        except AllProvidersFailedError:
            self._record_total_failure(mode, started, stream=True)
            raise
        self._metrics.time_to_first_token_seconds.labels(
            mode.value, primed.binding.provider_name
        ).observe(perf_counter() - started)
        return sse_stream(self._instrumented(primed, mode, started))

    def _instrumented(
        self, primed: PrimedStream, mode: LLMMode, started: float
    ) -> PrimedStream:
        """Tap the event flow to record stream metrics and the log line.

        The tap sees provider failures on their way to ``sse_stream`` (which
        emits the terminal error event), so it can count them and mark the
        request's status; usage is taken from the terminal event when the
        stream completes.
        """
        binding = primed.binding

        async def tapped() -> AsyncIterator[GatewayStreamEvent]:
            status = "success"
            usage = primed.first_event.usage
            try:
                async for event in primed.events:
                    if event.usage is not None:
                        usage = event.usage
                    yield event
            except ProviderError as exc:
                status = "stream_failed"
                self._on_error(mode)(exc)
                raise
            except Exception:
                status = "stream_failed"
                raise
            finally:
                self._record_request(
                    mode,
                    binding.provider_name,
                    binding.model,
                    status=status,
                    status_code=200,
                    elapsed_seconds=perf_counter() - started,
                    usage=usage,
                    stream=True,
                )

        return PrimedStream(
            binding=binding, first_event=primed.first_event, events=tapped()
        )

    # ---------------------------------------------------------------- embed

    async def embed(
        self, mode: LLMMode, inputs: list[str], *, estimated_prompt_tokens: int
    ) -> EmbedResult:
        """Step 7-11 for an embedding request."""
        started = perf_counter()

        async def run(
            binding: ProviderBinding,
        ) -> tuple[ProviderBinding, list[list[float]]]:
            return binding, await binding.embed(inputs)

        try:
            binding, vectors = await run_with_fallback(
                mode,
                self._router,
                run,
                delays=self._delays,
                sleep=self._sleep,
                on_fallback=self._on_fallback,
                on_error=self._on_error(mode),
            )
        except AllProvidersFailedError:
            self._record_total_failure(mode, started, stream=False)
            raise
        elapsed = perf_counter() - started
        usage = Usage(prompt_tokens=estimated_prompt_tokens)
        self._record_request(
            mode,
            binding.provider_name,
            binding.model,
            status="success",
            status_code=200,
            elapsed_seconds=elapsed,
            usage=usage,
        )
        return EmbedResult(
            embeddings=vectors,
            provider=binding.provider_name,
            model=binding.model,
            prompt_tokens=estimated_prompt_tokens,
            latency_ms=int(elapsed * 1000),
        )
