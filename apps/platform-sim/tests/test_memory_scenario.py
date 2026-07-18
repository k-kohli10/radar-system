"""The order-service memory-pressure scenario.

Same ``_Spike`` shape as the latency scenario and the same
:class:`AbsoluteChaosRequest` — bytes rather than seconds, which is the point:
the absolute model established for latency carries a second unit without change.

The one thing genuinely worth asserting beyond the usual pin/expire/reset is
that a large float survives the round trip intact. 2.5e9 exceeds a 32-bit int,
and Prometheus renders floats in exponential form, so this is where a silent
precision or formatting problem would surface rather than in the 0.0-1.0 gauges.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from radar_platform_sim.chaos import ChaosController
from radar_platform_sim.main import create_app

#: The measured spike from deploy/prometheus/alerting-rules.yml: 2.5GB against a
#: 1.5e9 threshold.
SPIKE_BYTES = 2.5e9


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


def test_memory_gauge_pins_at_the_spiked_byte_count() -> None:
    client = _client()

    assert _sample(client.get("/metrics").text, "order_service_memory_bytes") == 0.0

    client.post(
        "/chaos/order-memory",
        json={"value": SPIKE_BYTES, "duration_seconds": 300},
    )
    assert (
        _sample(client.get("/metrics").text, "order_service_memory_bytes")
        == SPIKE_BYTES
    )


def test_large_byte_values_survive_the_metrics_round_trip() -> None:
    """2.5e9 must come back exactly, not rounded or truncated.

    The rule compares against 1.5e9, so a value that lost precision on the way
    through the exposition format could sit on the wrong side of the threshold.
    """
    client = _client()
    client.post(
        "/chaos/order-memory",
        json={"value": SPIKE_BYTES, "duration_seconds": 300},
    )

    exposed = _sample(client.get("/metrics").text, "order_service_memory_bytes")

    assert exposed == SPIKE_BYTES
    assert exposed > 1.5e9, "must land above the OrderServiceHighMemory threshold"


def test_memory_auto_resets_when_its_deadline_passes() -> None:
    clock = FakeClock()
    chaos = ChaosController(clock=clock)

    chaos.spike_order_memory(SPIKE_BYTES, 300)
    assert chaos.order_memory_bytes() == SPIKE_BYTES

    clock.advance(299.0)
    assert chaos.order_memory_bytes() == SPIKE_BYTES, "still inside the window"

    clock.advance(2.0)
    assert chaos.order_memory_bytes() == 0.0, "past the deadline, back to baseline"


def test_reset_clears_the_memory_spike() -> None:
    client = _client()

    client.post(
        "/chaos/order-memory",
        json={"value": SPIKE_BYTES, "duration_seconds": 300},
    )
    client.post("/chaos/reset")

    assert _sample(client.get("/metrics").text, "order_service_memory_bytes") == 0.0


def test_reset_clears_all_five_scenarios() -> None:
    """Every scenario that exists, cleared by one reset.

    Each scenario wired its own clear() in its own commit; this is the assertion
    that none of them was missed along the way.
    """
    client = _client()

    client.post("/chaos/order-failures", json={"rate": 0.15, "duration_seconds": 120})
    client.post(
        "/chaos/checkout-timeouts", json={"rate": 0.35, "duration_seconds": 120}
    )
    client.post("/chaos/payment-errors", json={"rate": 0.15, "duration_seconds": 120})
    client.post(
        "/chaos/inventory-latency", json={"value": 1.5, "duration_seconds": 120}
    )
    client.post(
        "/chaos/order-memory", json={"value": SPIKE_BYTES, "duration_seconds": 300}
    )

    client.post("/chaos/reset")
    body = client.get("/metrics").text

    for gauge in (
        "order_processing_failure_rate",
        "checkout_timeout_rate",
        "payment_gateway_error_rate",
        "inventory_check_p95_seconds",
        "order_service_memory_bytes",
    ):
        assert _sample(body, gauge) == 0.0, f"{gauge} survived /chaos/reset"


def test_memory_endpoint_rejects_a_non_positive_value() -> None:
    response = _client().post(
        "/chaos/order-memory", json={"value": 0.0, "duration_seconds": 60}
    )
    assert response.status_code == 422
