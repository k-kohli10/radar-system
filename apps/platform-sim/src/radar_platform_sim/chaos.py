"""In-memory chaos state for the platform simulator.

A chaos endpoint spikes one of the two rate gauges for a bounded window. Rather
than spawn a background task to undo the spike after ``duration_seconds`` (extra
lifecycle, extra shutdown handling), a spike stores a *deadline* and the gauge
value is computed from it at scrape time: active while ``now < deadline``, back
to the ``0.0`` baseline once the deadline passes. Auto-reset is therefore free —
it happens by the deadline elapsing, with nothing to cancel or await.

Deadlines use :func:`time.monotonic` so they are immune to wall-clock jumps
(NTP steps, DST); only elapsed real time matters.

The controller is single-instance, mutated only inside request handlers on one
event loop with no ``await`` between read and write, so it needs no locking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

# The value both gauges hold when no chaos is active. No traffic, no failures.
BASELINE_RATE = 0.0


class ChaosRequest(BaseModel):
    """Body for the rate-gauge chaos endpoints.

    Used by ``POST /chaos/order-failures`` and ``/chaos/checkout-timeouts``.

    ``rate`` is the fraction to pin the target gauge at (0.0–1.0);
    ``duration_seconds`` is how long the spike lasts before it auto-resets.
    """

    rate: float = Field(ge=0.0, le=1.0)
    duration_seconds: int = Field(gt=0)


@dataclass
class _Spike:
    """One rate gauge's chaos state: a pinned ``rate`` until ``deadline``.

    ``deadline`` is a :func:`time.monotonic` timestamp; ``0.0`` means inactive
    (or already expired), which reads back as the baseline.
    """

    rate: float = BASELINE_RATE
    deadline: float = 0.0

    def effective_rate(self, now: float) -> float:
        """The gauge value at ``now``: the pinned rate if still within the
        window, else the baseline (auto-reset by an elapsed deadline)."""
        return self.rate if now < self.deadline else BASELINE_RATE

    def spike(self, rate: float, duration_seconds: int, now: float) -> None:
        self.rate = rate
        self.deadline = now + duration_seconds

    def clear(self) -> None:
        self.rate = BASELINE_RATE
        self.deadline = 0.0


@dataclass
class ChaosController:
    """Holds the chaos state for both rate gauges.

    Read the two ``*_rate`` methods at scrape time to reconcile the gauges; call
    the ``spike_*`` / :meth:`reset` methods from the chaos endpoints.
    """

    order_failures: _Spike = field(default_factory=_Spike)
    checkout_timeouts: _Spike = field(default_factory=_Spike)

    def spike_order_failures(self, rate: float, duration_seconds: int) -> None:
        self.order_failures.spike(rate, duration_seconds, time.monotonic())

    def spike_checkout_timeouts(self, rate: float, duration_seconds: int) -> None:
        self.checkout_timeouts.spike(rate, duration_seconds, time.monotonic())

    def reset(self) -> None:
        """Clear both spikes: gauges return to baseline immediately."""
        self.order_failures.clear()
        self.checkout_timeouts.clear()

    def order_failure_rate(self) -> float:
        """Current ``order_processing_failure_rate`` value."""
        return self.order_failures.effective_rate(time.monotonic())

    def checkout_timeout_rate(self) -> float:
        """Current ``checkout_timeout_rate`` value."""
        return self.checkout_timeouts.effective_rate(time.monotonic())
