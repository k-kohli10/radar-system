"""Gemini vendor specifics for the gateway provider layer.

The Gemini SDK adapter itself lives in ``plugins/llm/gemini``. This module is
the gateway-side knowledge about that vendor: which Vault secret holds its API
key and how its exceptions classify for retry.

:func:`translate_failure` returns classification only (:class:`FailureInfo`)
— it cannot return text. Vendor exception messages can echo prompt content,
so ``ProviderBinding`` drops them entirely (never truncated) and builds the
``ProviderError`` reason from the exception class name alone.

``google-generativeai`` surfaces call failures as
``google.api_core.exceptions.GoogleAPICallError`` subclasses. Mapping:

- ``DeadlineExceeded``     -> timeout=True. It IS a timeout, not a generic
  transient failure; ``ProviderBinding`` treats the two differently.
- ``RetryError``           -> timeout=True; api_core raises it when its own
  internal retry deadline is exhausted, i.e. the call timed out as a whole.
- other ``GoogleAPICallError`` -> its HTTP status; retryable iff in the
  spec's closed retry-on list (429, 500, 502, 503, 504)
- anything else (including safety blocks like ``BlockedPromptException``)
  -> unrecognized (non-retryable)

The ``code`` attribute on Google exceptions is an HTTP int for REST
transport but can be a gRPC ``StatusCode`` enum (whose ``value`` is a
``(number, text)`` tuple — and the number is a *gRPC* code, not an HTTP
status) depending on how the exception was constructed. :func:`_http_status`
therefore only trusts ``code`` when it is an ``int``; otherwise it falls back
to mapping the exception class itself, and never crashes on an odd ``code``.
"""

from __future__ import annotations

from google.api_core import exceptions as gexc

from .base import FailureInfo

PROVIDER_NAME = "gemini"
"""Registry/plugin name and the ``provider:`` value used in gateway config."""

API_KEY_SECRET = "gemini_api_key"
"""Vault secret filename holding the Gemini API key."""

_CLASS_HTTP_STATUS: tuple[tuple[type[gexc.GoogleAPICallError], int], ...] = (
    (gexc.TooManyRequests, 429),  # ResourceExhausted subclasses this
    (gexc.InternalServerError, 500),
    (gexc.BadGateway, 502),
    (gexc.ServiceUnavailable, 503),
    (gexc.GatewayTimeout, 504),
    (gexc.BadRequest, 400),
    (gexc.Unauthorized, 401),
    (gexc.Forbidden, 403),
)
"""HTTP status by exception class, used when ``code`` is not a plain int."""


def _http_status(exc: gexc.GoogleAPICallError) -> int | None:
    """Extract an HTTP status without assuming the type of ``exc.code``."""
    code = exc.code
    if isinstance(code, int):
        # Covers plain ints and IntEnums (http.HTTPStatus); a gRPC StatusCode
        # enum is NOT an int and must not be misread as an HTTP status.
        return int(code)
    for exc_class, status in _CLASS_HTTP_STATUS:
        if isinstance(exc, exc_class):
            return status
    return None


def translate_failure(exc: BaseException) -> FailureInfo | None:
    """Classify a Gemini/google-api-core exception for the retry policy."""
    # DeadlineExceeded subclasses GoogleAPICallError (code 504): check first so
    # it surfaces as a timeout, not a generic 504.
    if isinstance(exc, gexc.DeadlineExceeded):
        return FailureInfo(timeout=True)
    if isinstance(exc, gexc.RetryError):
        return FailureInfo(timeout=True)
    if isinstance(exc, gexc.GoogleAPICallError):
        return FailureInfo(status_code=_http_status(exc))
    return None
