"""Circuit breaker: open after repeated provider failures and fail fast.

Three layers of coverage:

- the :class:`CircuitBreaker` state machine directly (closed → open → half-open
  → closed/open), driven by an injected clock;
- ``call_with_retries`` and ``run_with_fallback`` integration — the breaker must
  stop the retry loop and skip the network call entirely;
- the wired gateway app — an open primary fails straight over to the fallback
  and the ``radar_llm_circuit_breaker_state`` gauge tracks the transitions.

The load-bearing guarantee — *an open circuit makes zero provider calls* — is
proven mutation-style: the identical scenario run without a breaker still burns
the full retry budget, so the fail-fast is the breaker's doing and nothing else.
"""

from __future__ import annotations

import pytest
from gateway_harness import GatewayEnv, build_harness, chat_body
from radar_contracts import LLMMode
from radar_llm_gateway.core.config import CircuitBreakerConfig
from radar_llm_gateway.core.errors import CircuitOpenError, ProviderError
from radar_llm_gateway.gateway.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitState,
)
from radar_llm_gateway.gateway.fallback import run_with_fallback
from radar_llm_gateway.gateway.model_router import ModelRouter
from radar_llm_gateway.gateway.retry import call_with_retries
from radar_llm_gateway.providers.base import ProviderBinding


async def _no_sleep(seconds: float) -> None:
    return None


def _state(breaker: CircuitBreaker) -> CircuitState:
    # Read the state through a function so mypy does not narrow the ``.state``
    # expression to a literal across the opaque mutating calls between asserts
    # (record_failure/record_success change it invisibly to the type checker).
    return breaker.state


def _binding(provider: str, model: str) -> ProviderBinding:
    # operation() below never touches chat/embedder, so leaving them unset is
    # fine: the binding is here only for its identity (provider/model) and its
    # breaker key.
    return ProviderBinding(
        provider_name=provider,
        model=model,
        timeout_seconds=5,
        translate=lambda exc: None,
    )


# ------------------------------------------------------ state machine (unit)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _breaker(threshold: int = 3, reset: float = 30.0) -> tuple[CircuitBreaker, _Clock]:
    clock = _Clock()
    breaker = CircuitBreaker(
        provider="openai",
        model="gpt-4o",
        failure_threshold=threshold,
        reset_timeout_seconds=reset,
        clock=clock,
    )
    return breaker, clock


def test_opens_after_threshold_consecutive_failures() -> None:
    breaker, _clock = _breaker(threshold=3)
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    assert _state(breaker) is CircuitState.CLOSED  # two failures, still closed
    assert breaker.allow()
    breaker.record_failure()  # the third
    assert _state(breaker) is CircuitState.OPEN
    assert not breaker.allow()  # open: calls fail fast


def test_success_resets_the_failure_count() -> None:
    breaker, _clock = _breaker(threshold=3)
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    breaker.record_success()  # a good call between failures
    assert _state(breaker) is CircuitState.CLOSED
    # Two fresh failures must not open it — the earlier two were cleared.
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    assert _state(breaker) is CircuitState.CLOSED


def test_half_open_trial_success_closes_the_circuit() -> None:
    breaker, clock = _breaker(threshold=1, reset=30.0)
    breaker.allow()
    breaker.record_failure()  # opens immediately (threshold 1)
    assert _state(breaker) is CircuitState.OPEN
    assert not breaker.allow()  # still cooling down

    clock.now = 30.0  # reset timeout elapsed
    assert breaker.allow()  # one trial permitted
    assert _state(breaker) is CircuitState.HALF_OPEN
    assert not breaker.allow()  # only one trial at a time
    breaker.record_success()
    assert _state(breaker) is CircuitState.CLOSED


def test_half_open_trial_failure_reopens_and_restarts_timer() -> None:
    breaker, clock = _breaker(threshold=1, reset=30.0)
    breaker.allow()
    breaker.record_failure()
    clock.now = 30.0
    assert breaker.allow()  # half-open trial
    breaker.record_failure()  # trial fails
    assert _state(breaker) is CircuitState.OPEN
    assert not breaker.allow()  # timer restarted from now (30.0)
    clock.now = 60.0
    assert breaker.allow()  # trial again after another full cooldown


def test_state_change_hook_fires_on_every_transition() -> None:
    seen: list[CircuitState] = []
    clock = _Clock()
    breaker = CircuitBreaker(
        provider="openai",
        model="gpt-4o",
        failure_threshold=1,
        reset_timeout_seconds=30.0,
        clock=clock,
        on_state_change=seen.append,
    )
    breaker.allow()
    breaker.record_failure()  # closed -> open
    clock.now = 30.0
    breaker.allow()  # open -> half_open
    breaker.record_success()  # half_open -> closed
    assert seen == [CircuitState.OPEN, CircuitState.HALF_OPEN, CircuitState.CLOSED]


# --------------------------------------------------- retry-loop integration


async def test_open_circuit_fails_fast_without_calling_operation() -> None:
    breaker, _clock = _breaker(threshold=3)

    calls = 0

    async def dead() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError("openai", "gpt-4o", status_code=503)

    # First cycle trips the breaker on its third failure and stops (no 4th
    # attempt, no sleeping out the budget).
    with pytest.raises(ProviderError):
        await call_with_retries(dead, sleep=_no_sleep, breaker=breaker)
    assert calls == 3
    assert _state(breaker) is CircuitState.OPEN

    # Second cycle: the open circuit rejects before operation runs at all.
    with pytest.raises(CircuitOpenError):
        await call_with_retries(dead, sleep=_no_sleep, breaker=breaker)
    assert calls == 3  # unchanged: zero provider calls


async def test_without_breaker_the_same_scenario_burns_the_full_budget() -> None:
    # Mutation control for the test above: drop the breaker and the second
    # cycle makes four more calls. This is what proves the breaker — not some
    # other guard — is what produces the fail-fast.
    calls = 0

    async def dead() -> str:
        nonlocal calls
        calls += 1
        raise ProviderError("openai", "gpt-4o", status_code=503)

    for _ in range(2):
        with pytest.raises(ProviderError):
            await call_with_retries(dead, sleep=_no_sleep)
    assert calls == 8  # 4 + 4, no fail-fast


async def test_open_primary_fails_fast_to_the_fallback_binding() -> None:
    registry = CircuitBreakerRegistry(failure_threshold=3, reset_timeout_seconds=30.0)
    primary = _binding("openai", "gpt-4o")
    fallback = _binding("openai", "gpt-4o-mini")
    router = ModelRouter({LLMMode.EXTENDED: primary}, {LLMMode.EXTENDED: fallback})

    primary_calls = 0
    fallback_calls = 0

    async def operation(binding: ProviderBinding) -> str:
        nonlocal primary_calls, fallback_calls
        if binding is primary:
            primary_calls += 1
            raise ProviderError("openai", "gpt-4o", status_code=503)
        fallback_calls += 1
        return "answer from fallback"

    def breaker_for(binding: ProviderBinding) -> CircuitBreaker:
        return registry.get(binding.provider_name, binding.model)

    # Request 1: primary fails and trips its breaker, fallback serves it.
    result = await run_with_fallback(
        LLMMode.EXTENDED,
        router,
        operation,
        sleep=_no_sleep,
        breaker_for=breaker_for,
    )
    assert result == "answer from fallback"
    assert primary_calls == 3  # tripped on the third failure, no 4th attempt
    assert fallback_calls == 1

    # Request 2: primary circuit is open — it is skipped entirely.
    result = await run_with_fallback(
        LLMMode.EXTENDED,
        router,
        operation,
        sleep=_no_sleep,
        breaker_for=breaker_for,
    )
    assert result == "answer from fallback"
    assert primary_calls == 3  # unchanged: primary was never called
    assert fallback_calls == 2


# ---------------------------------------------------------- wired-app level


def test_app_open_circuit_serves_from_fallback_and_moves_the_gauge(
    gateway_env: GatewayEnv,
) -> None:
    gw = build_harness(
        gateway_env,
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=3, reset_timeout_seconds=30.0
        ),
    )
    gw.primary_chat.fail_times = 99  # openai/gpt-4o is down

    first = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert first.status_code == 200
    assert first.json()["model"] == "gpt-4o-mini"  # served by the fallback
    assert gw.primary_chat.calls == 3  # tripped, not the full 4
    assert (
        gw.metric("radar_llm_circuit_breaker_state", provider="openai", model="gpt-4o")
        == 1.0  # open
    )

    second = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert second.status_code == 200
    assert gw.primary_chat.calls == 3  # open circuit skipped the primary entirely


def test_app_half_open_recovery_serves_the_primary_again(
    gateway_env: GatewayEnv,
) -> None:
    clock = _Clock()
    gw = build_harness(
        gateway_env,
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=3, reset_timeout_seconds=30.0
        ),
        clock=clock,
    )
    gw.primary_chat.fail_times = 99
    gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert gw.primary_chat.calls == 3  # breaker now open

    gw.primary_chat.fail_times = 0  # provider recovers
    clock.now = 31.0  # reset timeout elapses

    recovered = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert recovered.status_code == 200
    assert recovered.json()["model"] == "gpt-4o"  # primary, via the half-open trial
    assert gw.primary_chat.calls == 4  # the single trial call
    assert (
        gw.metric("radar_llm_circuit_breaker_state", provider="openai", model="gpt-4o")
        == 0.0  # closed again
    )
