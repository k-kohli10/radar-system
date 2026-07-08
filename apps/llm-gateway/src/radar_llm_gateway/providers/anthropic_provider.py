"""Anthropic vendor specifics for the gateway provider layer.

The Anthropic SDK adapter itself lives in ``plugins/llm/anthropic``. This
module is the gateway-side knowledge about that vendor: which Vault secret
holds its API key and how its SDK exceptions classify for retry.

:func:`translate_failure` returns classification only (:class:`FailureInfo`)
— it cannot return text. Vendor exception messages can echo prompt content,
so ``ProviderBinding`` drops them entirely (never truncated) and builds the
``ProviderError`` reason from the exception class name alone.

Mapping (anthropic SDK exception taxonomy, same shape as openai's):

- ``APITimeoutError``      -> timeout (retryable, spec: "connection timeout")
- ``APIConnectionError``   -> retryable; a dropped connection is the same
  transient class as a connection timeout
- ``APIStatusError``       -> its HTTP status; retryable iff in the spec's
  closed retry-on list (429, 500, 502, 503, 504)
- anything else            -> unrecognized (non-retryable)
"""

from __future__ import annotations

import anthropic

from .base import FailureInfo

PROVIDER_NAME = "anthropic"
"""Registry/plugin name and the ``provider:`` value used in gateway config."""

API_KEY_SECRET = "anthropic_api_key"
"""Vault secret filename holding the Anthropic API key."""


def translate_failure(exc: BaseException) -> FailureInfo | None:
    """Classify an Anthropic SDK exception for the retry policy."""
    # Order matters: APITimeoutError subclasses APIConnectionError, and both
    # must be checked before the generic status branch.
    if isinstance(exc, anthropic.APITimeoutError):
        return FailureInfo(timeout=True)
    if isinstance(exc, anthropic.APIConnectionError):
        return FailureInfo(retryable=True)
    if isinstance(exc, anthropic.APIStatusError):
        # Includes 529, Anthropic's "overloaded" status. 529 is NOT in the
        # spec's closed retry-on list (429, 500, 502, 503, 504), so it is
        # non-retryable and falls through to the fallback provider by design —
        # not because retrying is wrong in theory, but because widening the
        # retry list requires an explicit spec decision, not a silent
        # assumption here.
        return FailureInfo(status_code=exc.status_code)
    return None
