"""Vendor exception translation and the ProviderBinding redaction guarantees."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator

import anthropic
import grpc
import httpx
import openai
import pytest
from gateway_harness import SECRET_VENDOR
from google.api_core import exceptions as gexc
from radar_contracts import (
    GatewayStreamEvent,
    LLMMode,
    LLMRequest,
    LLMResponse,
    Message,
)
from radar_llm_gateway.core.errors import ProviderError, ProviderTimeoutError
from radar_llm_gateway.providers import (
    anthropic_provider,
    gemini_provider,
    openai_provider,
)
from radar_llm_gateway.providers.base import (
    FailureInfo,
    FailureTranslator,
    ProviderBinding,
)

_REQUEST = httpx.Request("POST", "https://example.invalid/v1")


def _openai_status(cls: type[openai.APIStatusError], code: int) -> Exception:
    return cls(
        SECRET_VENDOR,
        response=httpx.Response(code, request=_REQUEST),
        body={"error": {"message": SECRET_VENDOR}},
    )


def _anthropic_status(cls: type[anthropic.APIStatusError], code: int) -> Exception:
    return cls(
        SECRET_VENDOR,
        response=httpx.Response(code, request=_REQUEST),
        body={"error": {"message": SECRET_VENDOR}},
    )


# ------------------------------------------------------------------ translators


@pytest.mark.parametrize(
    ("exc", "status", "timeout"),
    [
        (_openai_status(openai.RateLimitError, 429), 429, False),
        (_openai_status(openai.InternalServerError, 500), 500, False),
        (_openai_status(openai.BadRequestError, 400), 400, False),
        (_openai_status(openai.AuthenticationError, 401), 401, False),
        (openai.APITimeoutError(request=_REQUEST), None, True),
    ],
)
def test_openai_translation(exc: Exception, status: int | None, timeout: bool) -> None:
    info = openai_provider.translate_failure(exc)
    assert info is not None
    assert info.status_code == status
    assert info.timeout is timeout


def test_openai_connection_error_is_retryable() -> None:
    info = openai_provider.translate_failure(
        openai.APIConnectionError(message=SECRET_VENDOR, request=_REQUEST)
    )
    assert info == FailureInfo(retryable=True)


def test_anthropic_529_overloaded_is_non_retryable_by_design() -> None:
    # Anthropic maps >=500 to InternalServerError; a 529 arrives carrying its
    # real status. 529 is not in the closed retry list, so it goes straight
    # to fallback.
    overloaded = anthropic.InternalServerError(
        SECRET_VENDOR, response=httpx.Response(529, request=_REQUEST), body=None
    )
    info = anthropic_provider.translate_failure(overloaded)
    assert info is not None and info.status_code == 529
    error = ProviderError(
        "anthropic", "claude-sonnet-4-6", status_code=529, reason="InternalServerError"
    )
    assert error.retryable is False


def test_anthropic_regular_500_is_retryable() -> None:
    info = anthropic_provider.translate_failure(
        _anthropic_status(anthropic.InternalServerError, 500)
    )
    assert info is not None and info.status_code == 500


def test_gemini_deadline_exceeded_is_a_timeout_not_a_504() -> None:
    info = gemini_provider.translate_failure(gexc.DeadlineExceeded(SECRET_VENDOR))
    assert info is not None
    assert info.timeout is True
    assert info.status_code is None


def test_gemini_retry_error_is_a_timeout() -> None:
    info = gemini_provider.translate_failure(
        gexc.RetryError(SECRET_VENDOR, cause=gexc.ServiceUnavailable(SECRET_VENDOR))
    )
    assert info is not None and info.timeout is True


def test_gemini_grpc_enum_code_does_not_crash_or_misread() -> None:
    exc = gexc.ResourceExhausted(SECRET_VENDOR)
    exc.code = grpc.StatusCode.RESOURCE_EXHAUSTED  # value is (8, '...'), not HTTP
    info = gemini_provider.translate_failure(exc)
    assert info is not None
    assert info.status_code == 429  # class fallback, never the gRPC number 8

    unknown = gexc.GoogleAPICallError(SECRET_VENDOR)
    unknown.code = grpc.StatusCode.UNKNOWN
    info = gemini_provider.translate_failure(unknown)
    assert info is not None and info.status_code is None


@pytest.mark.parametrize(
    "translate",
    [
        openai_provider.translate_failure,
        anthropic_provider.translate_failure,
        gemini_provider.translate_failure,
    ],
)
def test_unrecognized_exceptions_are_unclassified(
    translate: FailureTranslator,
) -> None:
    assert translate(ValueError(SECRET_VENDOR)) is None


# ------------------------------------------------------------ binding behavior


class _RaisingChat:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise self._exc

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        raise self._exc
        yield GatewayStreamEvent(delta="")  # pragma: no cover


class _SlowChat:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        await asyncio.sleep(60)
        yield GatewayStreamEvent(delta="")  # pragma: no cover


_REQ = LLMRequest(mode=LLMMode.FAST, messages=[Message(role="user", content="q")])


@pytest.mark.parametrize(
    ("translate", "exc"),
    [
        (openai_provider.translate_failure, _openai_status(openai.RateLimitError, 429)),
        (
            anthropic_provider.translate_failure,
            _anthropic_status(anthropic.RateLimitError, 429),
        ),
        (gemini_provider.translate_failure, gexc.ResourceExhausted(SECRET_VENDOR)),
        (openai_provider.translate_failure, ValueError(SECRET_VENDOR)),
    ],
)
async def test_binding_strips_vendor_messages_entirely(
    translate: FailureTranslator, exc: Exception
) -> None:
    """The vendor exception message (which can echo prompt content) must be
    dropped entirely — not truncated — from the raised error and its
    traceback chain."""
    binding = ProviderBinding(
        provider_name="vendor",
        model="some-model",
        timeout_seconds=5,
        translate=translate,
        chat=_RaisingChat(exc),
    )
    with pytest.raises(ProviderError) as excinfo:
        await binding.complete(_REQ)
    rendered = "".join(traceback.format_exception(excinfo.value))
    assert SECRET_VENDOR not in rendered
    assert type(exc).__name__ in str(excinfo.value)  # class name only


async def test_binding_enforces_mode_timeout() -> None:
    binding = ProviderBinding(
        provider_name="openai",
        model="gpt-4o",
        timeout_seconds=0.05,
        translate=lambda exc: None,
        chat=_SlowChat(),
    )
    with pytest.raises(ProviderTimeoutError) as excinfo:
        await binding.complete(_REQ)
    assert excinfo.value.retryable is True


async def test_binding_capability_guard_is_a_bug_not_a_provider_error() -> None:
    binding = ProviderBinding(
        provider_name="openai",
        model="gpt-4o",
        timeout_seconds=5,
        translate=lambda exc: None,
        chat=_RaisingChat(ValueError("unused")),
    )
    with pytest.raises(RuntimeError):
        await binding.embed(["x"])
