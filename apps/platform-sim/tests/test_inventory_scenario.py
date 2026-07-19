"""The inventory-service latency scenario, and the guard on absolute values.

The gauge itself is the easy shape — pin a value, expire by deadline — so the
interesting assertions here are about the *request model*. Latency is an
absolute quantity in seconds, so it cannot reuse ``ChaosRequest``, whose
``le=1.0`` bound exists to reject a ratio passed as a percentage. These tests
pin down both halves of that split: absolute values above 1.0 are accepted here,
and are still rejected on the ratio endpoints.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from radar_platform_sim.chaos import ChaosController
from radar_platform_sim.main import create_app


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client() -> TestClient:
    return TestClient(create_app(metrics_registry=CollectorRegistry()))


def _sample(body: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)} (\S+)$", body, re.MULTILINE)
    assert match is not None, f"{name} not present in /metrics"
    return float(match.group(1))


def test_latency_gauge_pins_at_the_spiked_value() -> None:
    """1.5s is the value the measured contract says clears the 0.5s rule."""
    client = _client()

    assert _sample(client.get("/metrics").text, "inventory_check_p95_seconds") == 0.0

    client.post(
        "/chaos/inventory-latency", json={"value": 1.5, "duration_seconds": 120}
    )
    assert _sample(client.get("/metrics").text, "inventory_check_p95_seconds") == 1.5


def test_latency_auto_resets_when_its_deadline_passes() -> None:
    """Expiry needs no background task — the deadline elapsing is the reset."""
    clock = FakeClock()
    chaos = ChaosController(clock=clock)

    chaos.spike_inventory_latency(1.5, 120)
    assert chaos.inventory_check_p95() == 1.5

    clock.advance(119.0)
    assert chaos.inventory_check_p95() == 1.5, "still inside the window"

    clock.advance(2.0)
    assert chaos.inventory_check_p95() == 0.0, "past the deadline, back to baseline"


def test_reset_clears_the_latency_spike() -> None:
    client = _client()

    client.post(
        "/chaos/inventory-latency", json={"value": 1.5, "duration_seconds": 120}
    )
    client.post("/chaos/reset")

    assert _sample(client.get("/metrics").text, "inventory_check_p95_seconds") == 0.0


def test_absolute_endpoint_accepts_a_value_above_one() -> None:
    """The whole reason this endpoint does not reuse ChaosRequest."""
    response = _client().post(
        "/chaos/inventory-latency", json={"value": 2.5, "duration_seconds": 60}
    )
    assert response.status_code == 200


def test_ratio_endpoints_still_reject_a_value_above_one() -> None:
    """The le=1.0 guard survives: adding absolute values must not weaken it.

    A rate of 15 is almost certainly someone meaning 15%. Accepting it would pin
    the gauge at 15.0 and breach every ratio rule at once while looking like a
    successful spike.
    """
    client = _client()

    for endpoint in (
        "/chaos/order-failures",
        "/chaos/checkout-timeouts",
        "/chaos/payment-errors",
    ):
        response = client.post(endpoint, json={"rate": 15.0, "duration_seconds": 60})
        assert response.status_code == 422, f"{endpoint} accepted a rate of 15.0"


def test_latency_endpoint_rejects_a_non_positive_value() -> None:
    response = _client().post(
        "/chaos/inventory-latency", json={"value": 0.0, "duration_seconds": 60}
    )
    assert response.status_code == 422


def test_dead_inventory_histogram_is_gone() -> None:
    """It was declared but never observed, so it only ever rendered zeros.

    The scenario it looked like it served is now the p95 gauge.
    """
    body = _client().get("/metrics").text

    assert "inventory_check_duration_seconds" not in body
    assert "inventory_check_p95_seconds" in body
