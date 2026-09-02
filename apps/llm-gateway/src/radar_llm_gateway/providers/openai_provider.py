"""OpenAI vendor specifics for the gateway provider layer.

The OpenAI SDK adapter itself lives in ``plugins/llm/openai`` (vendor-neutral
contracts, vendor exceptions propagate). This module is the gateway-side
knowledge about that vendor: which Vault secret holds its API key and how its
SDK exceptions classify for retry. :func:`translate_failure` returns
classification only, never text; see ``providers/base.py`` for the redaction
rule that enforces.

Mapping (openai SDK exception taxonomy):

- ``APITimeoutError``      -> timeout (retryable, spec: "connection timeout")
- ``APIConnectionError``   -> retryable; a dropped connection is the same
  transient class as a connection timeout
- ``APIStatusError``       -> its HTTP status; retryable iff in the spec's
  closed retry-on list (429, 500, 502, 503, 504)
- anything else            -> unrecognized (non-retryable)
"""

from __future__ import annotations

import openai

from .base import FailureInfo

PROVIDER_NAME = "openai"
"""Registry/plugin name and the ``provider:`` value used in gateway config."""

API_KEY_SECRET = "openai_api_key"
"""Vault secret filename holding the OpenAI API key."""


def translate_failure(exc: BaseException) -> FailureInfo | None:
    """Classify an OpenAI SDK exception for the retry policy."""
    # Order matters: APITimeoutError subclasses APIConnectionError, and both
    # must be checked before the generic status branch.
    if isinstance(exc, openai.APITimeoutError):
        return FailureInfo(timeout=True)
    if isinstance(exc, openai.APIConnectionError):
        return FailureInfo(retryable=True)
    if isinstance(exc, openai.APIStatusError):
        return FailureInfo(status_code=exc.status_code)
    return None
