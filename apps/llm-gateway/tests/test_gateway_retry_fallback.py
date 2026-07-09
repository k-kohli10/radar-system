"""Retry policy (1s/3s/9s, retryable-only) and provider fallback behavior."""

from __future__ import annotations

import pytest
from gateway_harness import GatewayHarness, chat_body
from radar_llm_gateway.core.errors import (
    AllProvidersFailedError,
    ProviderError,
    ProviderTimeoutError,
)
from radar_llm_gateway.gateway.retry import RETRY_DELAYS_SECONDS, call_with_retries

# ------------------------------------------------------------------ retry unit


async def _no_sleep(seconds: float) -> None:
    return None


def test_retry_delays_match_spec() -> None:
    assert RETRY_DELAYS_SECONDS == (1.0, 3.0, 9.0)


async def test_retries_recover_after_transient_failures() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderError("openai", "gpt-4o", status_code=503)
        return "ok"

    assert await call_with_retries(flaky, sleep=record) == "ok"
    assert calls == 3
    assert sleeps == [1.0, 3.0]


async def test_retry_budget_is_four_attempts_with_full_backoff() -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def dead() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError("openai", "gpt-4o", status_code=503)

    with pytest.raises(ProviderError):
        await call_with_retries(dead, sleep=record)
    assert calls == 4
    assert sleeps == [1.0, 3.0, 9.0]


async def test_non_retryable_status_fails_on_first_attempt() -> None:
    calls = 0

    async def bad_request() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError("openai", "gpt-4o", status_code=400)

    with pytest.raises(ProviderError):
        await call_with_retries(bad_request, sleep=_no_sleep)
    assert calls == 1


async def test_timeouts_are_retryable() -> None:
    calls = 0

    async def times_out() -> str:
        nonlocal calls
        calls += 1
        raise ProviderTimeoutError("openai", "gpt-4o", 5.0)

    with pytest.raises(ProviderTimeoutError):
        await call_with_retries(times_out, sleep=_no_sleep)
    assert calls == 4


async def test_non_provider_errors_propagate_untouched() -> None:
    async def bug() -> str:
        raise ValueError("a gateway bug, not a provider failure")

    with pytest.raises(ValueError):
        await call_with_retries(bug, sleep=_no_sleep)


async def test_on_error_fires_once_per_failing_attempt() -> None:
    seen: list[int | None] = []

    async def dead() -> str:
        raise ProviderError("openai", "gpt-4o", status_code=503)

    with pytest.raises(ProviderError):
        await call_with_retries(
            dead, sleep=_no_sleep, on_error=lambda exc: seen.append(exc.status_code)
        )
    assert seen == [503, 503, 503, 503]


# ------------------------------------------------------- fallback via the app


def test_fallback_triggered_after_primary_exhausted(gw: GatewayHarness) -> None:
    gw.primary_chat.fail_times = 99
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-4o-mini"
    assert gw.primary_chat.calls == 4
    assert gw.fallback_chat.calls == 1
    assert gw.sleeps == [1.0, 3.0, 9.0]
    assert (
        gw.metric(
            "radar_llm_fallback_total", from_provider="openai", to_provider="openai"
        )
        == 1
    )
    assert (
        gw.metric(
            "radar_llm_provider_errors_total",
            mode="extended",
            provider="openai",
            error="FakeVendorError",
        )
        == 4
    )


def test_non_retryable_primary_fails_over_without_backoff(
    gw: GatewayHarness,
) -> None:
    gw.primary_chat.fail_times = 99
    gw.primary_chat.fail_status = 401
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert response.status_code == 200
    assert gw.primary_chat.calls == 1
    assert gw.fallback_chat.calls == 1
    assert gw.sleeps == []


def test_503_when_primary_and_fallback_both_exhausted(gw: GatewayHarness) -> None:
    gw.primary_chat.fail_times = 99
    gw.fallback_chat.fail_times = 99
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert response.status_code == 503
    assert gw.primary_chat.calls == 4
    assert gw.fallback_chat.calls == 4
    assert gw.sleeps == [1.0, 3.0, 9.0, 1.0, 3.0, 9.0]
    assert "gpt-4o" in response.json()["detail"]


def test_503_when_mode_has_no_fallback(gw: GatewayHarness) -> None:
    gw.primary_chat.fail_times = 99
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="fast"), headers=gw.fast_headers()
    )
    assert response.status_code == 503
    assert gw.primary_chat.calls == 4
    assert gw.fallback_chat.calls == 0


async def test_all_providers_failed_error_lists_every_provider_tried(
    gw: GatewayHarness,
) -> None:
    gw.primary_chat.fail_times = 99
    gw.fallback_chat.fail_times = 99
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    detail = response.json()["detail"]
    assert "openai/gpt-4o" in detail and "openai/gpt-4o-mini" in detail
    # sanity: the exception type itself carries the same data
    error = AllProvidersFailedError("extended", ["openai/gpt-4o"])
    assert error.mode == "extended"
