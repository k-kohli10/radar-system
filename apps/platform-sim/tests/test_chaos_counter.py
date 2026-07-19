"""The decline counter's ramp: the one metric that must evolve, not hold.

Every other scenario is a gauge, and a gauge is a pure function of its deadline
— read it twice with no time passing and you get the same number, which is
correct. A counter is the opposite: what the alert rule reads is
``rate(payment_declines_total[2m])``, the *slope*, so a counter that held still
between scrapes would render the rule permanently unfirable while looking
perfectly healthy on a dashboard.

So these tests are about time and arithmetic, not about endpoint shape. They
drive :class:`ChaosController` with an injected clock rather than sleeping: the
properties under test are all "what happened between t and t+n", and sleeping to
observe them would be slow and flaky for no added confidence.
"""

from __future__ import annotations

from radar_platform_sim.chaos import ChaosController


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _controller() -> tuple[ChaosController, FakeClock]:
    clock = FakeClock()
    return ChaosController(clock=clock), clock


def test_counter_climbs_between_two_scrapes() -> None:
    """Two scrapes a window apart must show an increase — the core property.

    This is what makes rate() non-zero. If this fails, the alert rule can never
    breach no matter how long chaos runs.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=300)

    clock.advance(15.0)
    first = chaos.drain_payment_declines()
    clock.advance(15.0)
    second = chaos.drain_payment_declines()

    assert first == 150, f"15s at 10/s should yield 150 declines, got {first}"
    assert second == 150, f"the next 15s should yield another 150, got {second}"


def test_drain_is_destructive_so_the_same_events_are_never_counted_twice() -> None:
    """Draining hands over progress; draining again with no time elapsed owes 0.

    A counter that re-handed the same accrual on every scrape would inflate with
    scrape frequency rather than with elapsed time.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=300)

    clock.advance(15.0)
    assert chaos.drain_payment_declines() == 150
    assert chaos.drain_payment_declines() == 0
    assert chaos.drain_payment_declines() == 0


def test_counter_never_goes_backwards_across_the_whole_lifecycle() -> None:
    """Monotonicity, asserted over ramp -> expiry -> reset -> re-ramp.

    A decreasing counter tells Prometheus the process restarted, and rate()
    discards the interval. That would silently break the rule, so this walks the
    full lifecycle and asserts no drain is ever negative.
    """
    chaos, clock = _controller()
    total = 0

    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=60)
    for _ in range(8):  # past the 60s deadline
        clock.advance(15.0)
        owed = chaos.drain_payment_declines()
        assert owed >= 0, "a drain must never be negative"
        total += owed

    chaos.reset()
    after_reset = total
    clock.advance(60.0)
    total += chaos.drain_payment_declines()
    assert total == after_reset, "reset must stop the climb"

    chaos.ramp_payment_declines(per_second=5.0, duration_seconds=60)
    clock.advance(20.0)
    owed = chaos.drain_payment_declines()
    assert owed >= 0
    assert total + owed > total, "a fresh ramp must resume climbing"


def test_ramp_stops_accruing_at_its_deadline() -> None:
    """Expiry is what bounds the spike, with no background task to cancel.

    60s at 10/s is 600 declines and not one more, however long the process then
    sits idle.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=60)

    clock.advance(600.0)  # ten minutes, but the window was one
    assert chaos.drain_payment_declines() == 600
    clock.advance(600.0)
    assert chaos.drain_payment_declines() == 0


def test_fractional_accrual_is_carried_not_lost() -> None:
    """Whole events only, with the remainder carried across drains.

    At 0.5/s a 1-second scrape interval owes half a decline. Rounding that to
    zero every time would stall the counter forever; rounding up would invent
    events. Ten 1s drains must total exactly 5.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=0.5, duration_seconds=300)

    total = 0
    for _ in range(10):
        clock.advance(1.0)
        total += chaos.drain_payment_declines()

    assert total == 5, f"10s at 0.5/s must be exactly 5 declines, got {total}"


def test_reramping_mid_window_does_not_drop_accrued_events() -> None:
    """Retargeting settles the old rate first.

    Without the settle in ``ramp()``, the accrual since the last drain is
    discarded when ``last_advance`` moves — events that already happened would
    vanish.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=300)

    clock.advance(10.0)  # 100 declines accrued, not yet drained
    chaos.ramp_payment_declines(per_second=1.0, duration_seconds=300)
    clock.advance(10.0)  # 10 more at the new rate

    assert chaos.drain_payment_declines() == 110


def test_reset_keeps_the_total_rather_than_rewinding_it() -> None:
    """Reset stops the ramp but must not rewind the counter.

    Rewinding would fake a process restart to rate(). Whatever accrued before
    the reset is real and still owed to the counter.
    """
    chaos, clock = _controller()
    chaos.ramp_payment_declines(per_second=10.0, duration_seconds=300)

    clock.advance(10.0)  # 100 accrued, undrained
    chaos.reset()

    assert chaos.drain_payment_declines() == 100, (
        "declines earned before the reset are still owed to the counter"
    )
