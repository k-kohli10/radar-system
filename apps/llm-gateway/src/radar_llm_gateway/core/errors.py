"""Gateway error types for provider failures.

Provider adapters translate vendor SDK exceptions into this vocabulary at the
provider boundary, so retry, fallback, and the API layer never see a vendor
type. The retry policy is data here (:data:`RETRYABLE_STATUS_CODES`), taken
verbatim from the spec:

- Retry on: 429, 500, 502, 503, 504, connection timeout
- Never retry on: 400, 401, 403, 422
- After retries: try the fallback provider if configured
- If the fallback also fails: 503 to the caller (:class:`AllProvidersFailedError`)

Everything roots in :class:`radar_common.UpstreamServiceError` so platform-wide
handling and the ``radar_errors_total`` metric see gateway failures uniformly.

Redaction rule for anyone raising these: build ``reason`` from metadata only,
meaning an HTTP status, a vendor exception class name, or the word "timeout".
Never pass a vendor exception's message: provider error bodies can echo request
content, and these error strings end up in logs and 5xx details.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from radar_common import UpstreamServiceError

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
"""Provider HTTP statuses worth retrying, per the spec's retry policy."""

NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 422})
"""Provider HTTP statuses that must never be retried."""


def is_retryable_status(status_code: int) -> bool:
    """Whether a provider HTTP status is in the spec's retry-on list.

    Anything not explicitly listed as retryable is not retried: the policy is
    a closed list, so an unexpected status falls through to fallback rather
    than burning retry budget.
    """
    return status_code in RETRYABLE_STATUS_CODES


class ProviderError(UpstreamServiceError):
    """A single provider call failed.

    ``retryable`` drives the retry loop. When not given explicitly it is
    derived from ``status_code``; a failure with neither (an unclassified SDK
    error) is not retryable.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        reason: str | None = None,
    ) -> None:
        if retryable is None:
            retryable = status_code is not None and is_retryable_status(status_code)
        detail = f"provider {provider}/{model} failed"
        if status_code is not None:
            detail += f" (status={status_code})"
        if reason:
            detail += f": {reason}"
        super().__init__(detail)
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.retryable = retryable
        self.reason = reason


class CircuitOpenError(ProviderError):
    """The binding's circuit breaker is open; the call was not attempted.

    A :class:`ProviderError` subclass marked non-retryable so it flows through
    the same retry/fallback pipeline: the retry loop stops immediately and
    ``run_with_fallback`` moves to the fallback binding, skipping the network
    call and its backoff.
    """

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(provider, model, retryable=False, reason="circuit_open")


class ProviderTimeoutError(ProviderError):
    """A provider call exceeded the mode's hard timeout or timed out connecting.

    Always retryable ("connection timeout" is in the spec's retry-on list).
    """

    def __init__(self, provider: str, model: str, timeout_seconds: float) -> None:
        super().__init__(
            provider,
            model,
            retryable=True,
            reason=f"timed out after {timeout_seconds}s",
        )
        self.timeout_seconds = timeout_seconds


class AllProvidersFailedError(UpstreamServiceError):
    """Primary retries and any configured fallback are exhausted; maps to 503.

    ``providers`` lists what was tried, e.g. ``["openai/gpt-4o",
    "openai/gpt-4o-mini"]``, for the error detail and logs.
    """

    def __init__(self, mode: str, providers: list[str]) -> None:
        super().__init__(
            f"all providers failed for mode '{mode}': tried {', '.join(providers)}"
        )
        self.mode = mode
        self.providers = providers


def install_error_handlers(app: FastAPI) -> None:
    """Map gateway errors that escape the routes to their HTTP responses.

    :class:`AllProvidersFailedError` is the spec's terminal 503. A bare
    :class:`ProviderError` reaching the edge means a code path skipped the
    retry/fallback pipeline; it still answers 503 rather than a 500 with a
    stack trace.
    """

    @app.exception_handler(AllProvidersFailedError)
    async def _all_providers_failed(
        request: Request, exc: AllProvidersFailedError
    ) -> Response:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> Response:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
        )
