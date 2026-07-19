"""In-memory chaos state for the platform simulator.

A chaos endpoint spikes one metric for a bounded window. Rather than spawn a
background task to undo the spike after ``duration_seconds`` (extra lifecycle,
extra shutdown handling), a spike stores a *deadline* and the metric is computed
from it at scrape time. Auto-reset is therefore free — it happens by the
deadline elapsing, with nothing to cancel or await.

There are two shapes here, and the difference is the whole of the interesting
part:

:class:`_Spike` — for gauges. A gauge *holds* a value, so the spike stores the
value and hands it back while the deadline is in the future, baseline after.
Reading it twice changes nothing; it is a pure function of the deadline.

:class:`_CounterRamp` — for counters. A counter must *evolve*: it may only ever
increase, and what a rule reads is ``rate()``, the slope. So it cannot be
computed as "the value right now" — there is no such thing. Instead the ramp
accrues ``per_second × elapsed`` over the time actually spent inside the active
window, and each drain hands the caller the whole units owed since the previous
drain. Two scrapes a few seconds apart therefore see a genuinely larger number,
which is what makes ``rate()`` non-zero. Draining is destructive by design:
what has been handed to the ``Counter`` is never handed out twice.

Deadlines use a monotonic clock so they are immune to wall-clock jumps (NTP
steps, DST); only elapsed real time matters. The clock is injectable on
:class:`ChaosController` so tests can advance time deterministically instead of
sleeping — the ramp's correctness is entirely about elapsed time, and a test
that slept to observe it would be both slow and flaky.

The controller is single-instance, mutated only inside request handlers on one
event loop with no ``await`` between read and write, so it needs no locking.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# The value every chaos-driven gauge holds when no chaos is active. No traffic,
# no failures, no latency.
BASELINE_VALUE = 0.0


class ChaosRequest(BaseModel):
    """Body for the rate-gauge chaos endpoints.

    Used by ``POST /chaos/order-failures``, ``/chaos/checkout-timeouts`` and
    ``/chaos/payment-errors``.

    ``rate`` is the fraction to pin the target gauge at (0.0–1.0);
    ``duration_seconds`` is how long the spike lasts before it auto-resets.
    """

    rate: float = Field(ge=0.0, le=1.0)
    duration_seconds: int = Field(gt=0)


class CounterRampRequest(BaseModel):
    """Body for the counter-ramp chaos endpoints (``/chaos/payment-declines``).

    ``per_second`` is the rate the counter climbs at while the ramp is active —
    events per second, not a fraction, so it is deliberately *not* capped at 1.0
    the way :class:`ChaosRequest` is. A rule reading this metric matches on
    ``rate()``, so ``per_second`` is what it ends up comparing to its threshold.
    """

    per_second: float = Field(gt=0.0)
    duration_seconds: int = Field(gt=0)


class AbsoluteChaosRequest(BaseModel):
    """Body for gauges holding an absolute quantity rather than a fraction.

    Used by ``POST /chaos/inventory-latency`` (seconds) and
    ``POST /chaos/order-memory`` (bytes).

    Deliberately a separate model rather than a loosened :class:`ChaosRequest`.
    That ``le=1.0`` bound is doing real work for the ratio scenarios: it rejects
    ``{"rate": 15}`` from someone who meant 15%, which would otherwise pin the
    gauge at 15.0 and breach every ratio rule at once while looking like a
    successful spike. Absolute quantities have no comparable natural ceiling —
    1.5 seconds and 2.5e9 bytes are both ordinary — so sharing one model would
    mean dropping a guard that catches a real mistake in order to accommodate
    values that never needed it.

    ``value`` is in the target metric's own unit; the endpoint says which.
    """

    value: float = Field(gt=0.0)
    duration_seconds: int = Field(gt=0)


@dataclass
class _Spike:
    """One gauge's chaos state: a pinned ``value`` until ``deadline``.

    The field is ``value``, not ``rate``: this same shape now backs both the
    0.0–1.0 ratio gauges and absolute gauges like inventory latency in seconds,
    so naming it ``rate`` would be a lie for half its uses. What it holds is
    whatever unit the target gauge is in.

    ``deadline`` is a :func:`time.monotonic` timestamp; ``0.0`` means inactive
    (or already expired), which reads back as the baseline.
    """

    value: float = BASELINE_VALUE
    deadline: float = 0.0

    def effective_value(self, now: float) -> float:
        """The gauge value at ``now``: the pinned value if still within the
        window, else the baseline (auto-reset by an elapsed deadline)."""
        return self.value if now < self.deadline else BASELINE_VALUE

    def spike(self, value: float, duration_seconds: int, now: float) -> None:
        self.value = value
        self.deadline = now + duration_seconds

    def clear(self) -> None:
        self.value = BASELINE_VALUE
        self.deadline = 0.0


@dataclass
class _CounterRamp:
    """One counter's chaos state: climb at ``per_second`` until ``deadline``.

    Unlike :class:`_Spike` this holds *unhanded* progress. ``pending`` is what
    has accrued but not yet been given to the ``Counter``; :meth:`drain` hands
    over the whole units and keeps the fraction, so the counter only ever
    advances by whole events and no fraction is lost to rounding across drains.

    ``last_advance`` is the monotonic timestamp accrual has been settled up to.
    """

    per_second: float = 0.0
    deadline: float = 0.0
    last_advance: float = 0.0
    pending: float = 0.0

    def _accrue(self, now: float) -> None:
        """Settle accrual up to ``now``, counting only time inside the window."""
        # Cap at the deadline: time after the ramp expired accrues nothing, which
        # is what makes expiry stop the climb without a background task.
        end = min(now, self.deadline)
        if end > self.last_advance:
            self.pending += self.per_second * (end - self.last_advance)
            self.last_advance = end

    def ramp(self, per_second: float, duration_seconds: int, now: float) -> None:
        """Start (or retarget) the ramp at ``per_second`` for the given window."""
        # Settle the outstanding accrual against the OLD rate before retargeting.
        # Without this, re-ramping mid-window silently drops whatever had accrued
        # since the last drain, because last_advance is about to move to `now`.
        self._accrue(now)
        self.per_second = per_second
        self.deadline = now + duration_seconds
        self.last_advance = now

    def drain(self, now: float) -> int:
        """Whole events owed to the ``Counter`` since the last drain.

        Destructive: what this returns has been handed over and will not be
        returned again. The caller must actually apply it, or those events are
        lost — a counter cannot be re-derived from state the way a gauge can.
        """
        self._accrue(now)
        whole = math.floor(self.pending)
        self.pending -= whole
        return int(whole)

    def clear(self, now: float) -> None:
        """Stop the climb. Accrual already earned is kept, not discarded.

        Deliberately does NOT rewind the counter. Rewinding a Prometheus counter
        means "the process restarted" — ``rate()`` treats a decrease as a reset
        and discounts the interval. Faking that on a chaos reset would corrupt
        the very query the alert rule runs, so reset stops the ramp and leaves
        the total standing, exactly as a real service's counter would behave
        once its incident ended.
        """
        self._accrue(now)
        self.per_second = 0.0
        self.deadline = 0.0
        self.last_advance = 0.0


@dataclass
class ChaosController:
    """Holds the chaos state for every scenario the simulator can fire.

    Read the ``*_rate`` methods and :meth:`drain_payment_declines` at scrape
    time to reconcile the metrics; call the ``spike_*`` / ``ramp_*`` /
    :meth:`reset` methods from the chaos endpoints.

    ``clock`` returns monotonic seconds and is injectable so tests can advance
    time without sleeping.
    """

    order_failures: _Spike = field(default_factory=_Spike)
    checkout_timeouts: _Spike = field(default_factory=_Spike)
    payment_errors: _Spike = field(default_factory=_Spike)
    payment_declines: _CounterRamp = field(default_factory=_CounterRamp)
    inventory_latency: _Spike = field(default_factory=_Spike)
    order_memory: _Spike = field(default_factory=_Spike)
    clock: Callable[[], float] = time.monotonic

    def spike_order_failures(self, rate: float, duration_seconds: int) -> None:
        self.order_failures.spike(rate, duration_seconds, self.clock())

    def spike_checkout_timeouts(self, rate: float, duration_seconds: int) -> None:
        self.checkout_timeouts.spike(rate, duration_seconds, self.clock())

    def spike_payment_errors(self, rate: float, duration_seconds: int) -> None:
        self.payment_errors.spike(rate, duration_seconds, self.clock())

    def ramp_payment_declines(self, per_second: float, duration_seconds: int) -> None:
        self.payment_declines.ramp(per_second, duration_seconds, self.clock())

    def spike_inventory_latency(self, seconds: float, duration_seconds: int) -> None:
        self.inventory_latency.spike(seconds, duration_seconds, self.clock())

    def spike_order_memory(self, memory_bytes: float, duration_seconds: int) -> None:
        self.order_memory.spike(memory_bytes, duration_seconds, self.clock())

    def reset(self) -> None:
        """Clear every scenario's chaos: gauges return to baseline immediately.

        The decline counter stops climbing but keeps its total — see
        :meth:`_CounterRamp.clear` for why rewinding it would be wrong.
        """
        now = self.clock()
        self.order_failures.clear()
        self.checkout_timeouts.clear()
        self.payment_errors.clear()
        self.payment_declines.clear(now)
        self.inventory_latency.clear()
        self.order_memory.clear()

    def order_failure_rate(self) -> float:
        """Current ``order_processing_failure_rate`` value."""
        return self.order_failures.effective_value(self.clock())

    def checkout_timeout_rate(self) -> float:
        """Current ``checkout_timeout_rate`` value."""
        return self.checkout_timeouts.effective_value(self.clock())

    def payment_error_rate(self) -> float:
        """Current ``payment_gateway_error_rate`` value."""
        return self.payment_errors.effective_value(self.clock())

    def inventory_check_p95(self) -> float:
        """Current ``inventory_check_p95_seconds`` value, in seconds."""
        return self.inventory_latency.effective_value(self.clock())

    def order_memory_bytes(self) -> float:
        """Current ``order_service_memory_bytes`` value, in bytes."""
        return self.order_memory.effective_value(self.clock())

    def drain_payment_declines(self) -> int:
        """Whole declines to add to ``payment_declines_total`` since last call.

        Destructive — the caller must apply the result to the counter.
        """
        return self.payment_declines.drain(self.clock())
