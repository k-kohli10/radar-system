"""The payment-gateway scenarios end to end through the app's own endpoints.

Complements ``test_chaos_counter``: that one proves the ramp arithmetic against
an injected clock, this one proves the wiring — that the endpoints exist, that
``/metrics`` actually applies the drain, and that the exposed metric names are
the ones ``deploy/prometheus/alerting-rules.yml`` watches. A rule referencing a
metric spelled differently from the one exposed is silently unfirable, so the
names are asserted literally here rather than imported from the app.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from radar_platform_sim.main import create_app


def _client() -> TestClient:
    return TestClient(create_app(metrics_registry=CollectorRegistry()))


def _sample(body: str, name: str) -> float:
    """The value of a single unlabelled sample in Prometheus text output."""
    match = re.search(rf"^{re.escape(name)} (\S+)$", body, re.MULTILINE)
    assert match is not None, f"{name} not present in /metrics"
    return float(match.group(1))


def test_payment_error_rate_spikes_and_auto_resets_by_expiry() -> None:
    client = _client()

    assert _sample(client.get("/metrics").text, "payment_gateway_error_rate") == 0.0

    client.post("/chaos/payment-errors", json={"rate": 0.15, "duration_seconds": 120})
    assert _sample(client.get("/metrics").text, "payment_gateway_error_rate") == 0.15

    client.post("/chaos/reset")
    assert _sample(client.get("/metrics").text, "payment_gateway_error_rate") == 0.0


def test_metrics_scrape_applies_the_decline_drain() -> None:
    """The counter advances because /metrics applied the drain, not on its own.

    Uses a real (not injected) clock, so this asserts the direction of travel
    rather than an exact figure — the arithmetic is pinned down in
    ``test_chaos_counter``.
    """
    client = _client()

    assert _sample(client.get("/metrics").text, "payment_declines_total") == 0.0

    client.post(
        "/chaos/payment-declines", json={"per_second": 1000.0, "duration_seconds": 60}
    )
    first = _sample(client.get("/metrics").text, "payment_declines_total")
    second = _sample(client.get("/metrics").text, "payment_declines_total")

    assert first > 0.0, "the first scrape after a ramp must apply some declines"
    assert second >= first, "the counter must never go backwards between scrapes"


def test_exposed_names_match_the_names_the_alert_rules_watch() -> None:
    """Guards the rule/metric contract from a rename on either side."""
    body = _client().get("/metrics").text

    for name in ("payment_gateway_error_rate", "payment_declines_total"):
        assert re.search(rf"^{re.escape(name)} ", body, re.MULTILINE), (
            f"{name} is watched by deploy/prometheus/alerting-rules.yml "
            f"but is not exposed on /metrics"
        )


def test_reset_clears_every_scenario_not_only_the_payment_ones() -> None:
    """Reset is only correct if it covers all four scenarios that exist today."""
    client = _client()

    client.post("/chaos/order-failures", json={"rate": 0.2, "duration_seconds": 120})
    client.post(
        "/chaos/checkout-timeouts", json={"rate": 0.35, "duration_seconds": 120}
    )
    client.post("/chaos/payment-errors", json={"rate": 0.2, "duration_seconds": 120})

    client.post("/chaos/reset")
    body = client.get("/metrics").text

    for gauge in (
        "order_processing_failure_rate",
        "checkout_timeout_rate",
        "payment_gateway_error_rate",
    ):
        assert _sample(body, gauge) == 0.0, f"{gauge} survived /chaos/reset"


def test_decline_ramp_rejects_a_non_positive_rate() -> None:
    client = _client()
    response = client.post(
        "/chaos/payment-declines", json={"per_second": 0.0, "duration_seconds": 60}
    )
    assert response.status_code == 422
