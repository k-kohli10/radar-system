"""Provider fallback: the second half of the failure policy.

Runs an operation against the mode's primary binding under the retry policy;
when the primary is exhausted (or fails non-retryably — a 400/401 on one
vendor can still succeed on the fallback's vendor or model), the configured
fallback binding gets its own full retry cycle. When both are spent — or the
primary fails with no fallback configured — :class:`AllProvidersFailedError`
raises, which the API layer answers with 503. The caller then degrades per
the platform policy (the Reasoner writes a template RCA with
``is_fallback=true``); the gateway itself never fabricates a completion.

The fallback transition is logged with metadata only and reported through
``on_fallback`` so the service layer can count
``radar_llm_fallback_total{from_provider, to_provider}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from radar_common import get_logger
from radar_contracts import LLMMode

from radar_llm_gateway.core.errors import AllProvidersFailedError, ProviderError
from radar_llm_gateway.gateway.model_router import ModelRouter
from radar_llm_gateway.gateway.retry import RETRY_DELAYS_SECONDS, call_with_retries
from radar_llm_gateway.providers.base import ProviderBinding

_log = get_logger(__name__)


async def run_with_fallback[T](
    mode: LLMMode,
    router: ModelRouter,
    operation: Callable[[ProviderBinding], Awaitable[T]],
    *,
    delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_fallback: Callable[[ProviderBinding, ProviderBinding], None] | None = None,
    on_error: Callable[[ProviderError], None] | None = None,
) -> T:
    """Run ``operation`` for ``mode``: primary with retries, then fallback.

    ``operation`` receives the binding to call (e.g. ``lambda b:
    b.complete(request)``) so the same policy serves completions and
    embeddings. ``delays``/``sleep`` are injectable for tests; ``on_error``
    fires once per failing provider call across both retry cycles.
    """
    primary = router.primary_for(mode)
    tried = [f"{primary.provider_name}/{primary.model}"]
    try:
        return await call_with_retries(
            lambda: operation(primary), delays=delays, sleep=sleep, on_error=on_error
        )
    except ProviderError as primary_exc:
        fallback = router.fallback_for(mode)
        if fallback is None:
            raise AllProvidersFailedError(mode.value, tried) from primary_exc
        _log.warning(
            "llm.provider_fallback",
            mode=mode.value,
            from_provider=primary.provider_name,
            from_model=primary.model,
            to_provider=fallback.provider_name,
            to_model=fallback.model,
            primary_status_code=primary_exc.status_code,
            primary_reason=primary_exc.reason,
        )
        if on_fallback is not None:
            on_fallback(primary, fallback)
        tried.append(f"{fallback.provider_name}/{fallback.model}")
        try:
            return await call_with_retries(
                lambda: operation(fallback),
                delays=delays,
                sleep=sleep,
                on_error=on_error,
            )
        except ProviderError as fallback_exc:
            raise AllProvidersFailedError(mode.value, tried) from fallback_exc
