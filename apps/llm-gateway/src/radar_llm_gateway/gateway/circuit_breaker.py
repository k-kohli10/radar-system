"""Per-provider circuit breaker: fail fast when a provider is down.

The retry policy (retry.py) and provider fallback (fallback.py) already handle
*transient* failures. What they cannot handle well is a provider that is simply
*down*: every request then burns its full retry budget — four calls with
1s+3s+9s of backoff — before failing over, so a hard outage turns each request
into 13s of dead waiting on a provider that was never going to answer.

The circuit breaker closes that gap. It tracks consecutive failures per binding
(provider + model) and, once a binding has failed :data:`failure_threshold`
times in a row, **opens**: subsequent calls to that binding are rejected
immediately with :class:`CircuitOpenError` — no network call, no backoff — so
``run_with_fallback`` moves straight to the fallback binding (and, if that is
also open, straight to the 503 that drives the Reasoner's template RCA).

States, per binding:

- **closed** — normal operation; calls flow and failures are counted.
- **open** — calls fail fast. After :data:`reset_timeout_seconds` the next
  call is allowed through as a single trial (half-open).
- **half-open** — exactly one trial call is permitted. Success closes the
  circuit and resets the count; failure reopens it and restarts the timer.
  Concurrent callers while a trial is in flight are rejected.

Keyed per binding, not per provider: a mode whose fallback is a *different*
model on the same vendor (the default openai/gpt-4o → openai/gpt-4o-mini)
still fails over to a healthy sibling model instead of being pre-empted by the
primary's open circuit. When primary and fallback share a vendor that is wholly
down, each opens on its own and the mode reaches 503 fast regardless.

The breaker holds no locks: the gateway is single-threaded asyncio and every
method here runs to completion without awaiting, so state transitions are
atomic with respect to other coroutines. ``allow`` marks a half-open trial in
flight precisely so an ``await``-suspended trial is not double-issued.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

from radar_common import get_logger

_log = get_logger(__name__)

DEFAULT_FAILURE_THRESHOLD = 5
"""Consecutive binding failures before the circuit opens."""

DEFAULT_RESET_TIMEOUT_SECONDS = 30.0
"""How long an open circuit waits before allowing a half-open trial call."""


class CircuitState(StrEnum):
    """The three states of a per-binding circuit."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Consecutive-failure circuit breaker for one provider binding.

    ``clock`` is injectable (monotonic seconds) so tests drive the open →
    half-open transition without real time. ``on_state_change`` fires on every
    state transition so the service layer can move the circuit-state gauge.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_timeout_seconds: float = DEFAULT_RESET_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[CircuitState], None] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._clock = clock
        self._on_state_change = on_state_change
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def is_open(self) -> bool:
        """Whether the circuit is currently open (calls fail fast)."""
        return self._state is CircuitState.OPEN

    def allow(self) -> bool:
        """Whether a call may proceed now; has side effects on the state.

        Closed always allows. Open allows only once the reset timeout has
        elapsed, transitioning to half-open and reserving the single trial.
        Half-open allows exactly one in-flight trial; concurrent callers are
        rejected until that trial reports back via ``record_*``.
        """
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            assert self._opened_at is not None
            if self._clock() - self._opened_at >= self._reset_timeout:
                self._transition(CircuitState.HALF_OPEN)
                self._probe_in_flight = True
                return True
            return False
        # HALF_OPEN: one trial at a time.
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        """A call succeeded: close the circuit and reset the failure count."""
        self._probe_in_flight = False
        self._failures = 0
        self._opened_at = None
        if self._state is not CircuitState.CLOSED:
            self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """A call failed: count it, opening the circuit at the threshold.

        A failed half-open trial reopens immediately and restarts the timer.
        """
        self._probe_in_flight = False
        if self._state is CircuitState.HALF_OPEN:
            self._opened_at = self._clock()
            self._transition(CircuitState.OPEN)
            return
        if self._state is CircuitState.OPEN:
            # allow() gates OPEN, so a call should not reach here; be safe.
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()
            self._transition(CircuitState.OPEN)

    def _transition(self, new: CircuitState) -> None:
        if new is self._state:
            return
        old = self._state
        self._state = new
        _log.warning(
            "llm.circuit_breaker",
            provider=self.provider,
            model=self.model,
            from_state=old.value,
            to_state=new.value,
            failures=self._failures,
        )
        if self._on_state_change is not None:
            self._on_state_change(new)


class CircuitBreakerRegistry:
    """Lazily builds and holds one :class:`CircuitBreaker` per binding.

    Keyed by ``(provider, model)`` so every mode that routes to the same
    binding shares its circuit. ``on_state_change`` is re-bound with the
    binding's identity so the service layer can label its gauge.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_timeout_seconds: float = DEFAULT_RESET_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, str, CircuitState], None] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout_seconds
        self._clock = clock
        self._on_state_change = on_state_change
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    def get(self, provider: str, model: str) -> CircuitBreaker:
        key = (provider, model)
        breaker = self._breakers.get(key)
        if breaker is None:
            on_change: Callable[[CircuitState], None] | None = None
            if self._on_state_change is not None:
                notify = self._on_state_change

                def on_change(state: CircuitState) -> None:
                    notify(provider, model, state)

            breaker = CircuitBreaker(
                provider=provider,
                model=model,
                failure_threshold=self._failure_threshold,
                reset_timeout_seconds=self._reset_timeout,
                clock=self._clock,
                on_state_change=on_change,
            )
            self._breakers[key] = breaker
        return breaker
