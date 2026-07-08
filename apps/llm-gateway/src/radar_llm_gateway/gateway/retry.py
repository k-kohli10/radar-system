"""Retry with fixed backoff for provider calls.

The spec's policy, verbatim: retry 3 times with 1s, 3s, 9s backoff — i.e. an
initial attempt plus up to three retries (four calls total), sleeping before
each retry. Only failures classified retryable by the provider layer (429,
500, 502, 503, 504, timeouts, connection errors) are retried; anything else
— including a non-ProviderError, which is a bug rather than a provider
failure — propagates immediately. After the budget is exhausted the last
error is raised for the fallback layer to handle.

Retry attempts are logged with metadata only (provider, model, status code,
exception class name, attempt number) — never message content; the
``ProviderError`` reason is already redacted at the provider boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from radar_common import get_logger

from radar_llm_gateway.core.errors import ProviderError

RETRY_DELAYS_SECONDS: tuple[float, ...] = (1.0, 3.0, 9.0)
"""Backoff before each retry; the tuple length is the retry budget."""

_log = get_logger(__name__)


async def call_with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_error: Callable[[ProviderError], None] | None = None,
) -> T:
    """Run ``operation``, retrying retryable :class:`ProviderError` failures.

    ``delays`` and ``sleep`` are injectable for tests; production callers use
    the spec defaults. ``on_error`` fires once per failing attempt (whether or
    not it will be retried) so the service layer can count
    ``radar_llm_provider_errors_total``. Raises the final
    :class:`ProviderError` once the budget is exhausted (or immediately when
    the failure is not retryable).
    """
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except ProviderError as exc:
            if on_error is not None:
                on_error(exc)
            if not exc.retryable or attempt == attempts:
                raise
            delay = delays[attempt - 1]
            _log.warning(
                "llm.provider_retry",
                provider=exc.provider,
                model=exc.model,
                status_code=exc.status_code,
                reason=exc.reason,
                attempt=attempt,
                max_attempts=attempts,
                retry_in_seconds=delay,
            )
            await sleep(delay)
    raise AssertionError("unreachable: loop always returns or raises")
