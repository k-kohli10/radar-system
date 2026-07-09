"""The five Prometheus metrics the order-stub exposes.

These are e-commerce *domain* metrics for the simulated order-service, not the
``radar_*`` platform metrics that live in ``radar_telemetry`` — so they are
declared here, spelled exactly as the plan lists them. Rendering still reuses
``radar_telemetry.render_latest`` (see ``main.py``); nothing about metric
*output* is reimplemented.

Two of the five are gauges chaos drives: ``order_processing_failure_rate`` and
``checkout_timeout_rate``. The stub does not simulate order traffic, so the
counter and the two histograms are exposed but only ever observed if a caller
drives them; at rest they render at zero.

Following the ``radar_telemetry`` pattern, :func:`create_order_metrics` takes a
``registry`` (default: the global ``REGISTRY``) and returns a frozen bundle, so
tests or a second app instance can pass a fresh ``CollectorRegistry`` and avoid
duplicate-registration errors.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)


@dataclass(frozen=True)
class OrderMetrics:
    """The order-stub's five exposed metrics.

    ``processing_failure_rate`` and ``checkout_timeout_rate`` are 0.0–1.0 gauges
    reconciled from chaos state at scrape time (see ``main.py``). The rest are
    declared for scraping completeness and stay at zero unless observed.
    """

    processing_failure_rate: Gauge
    checkout_timeout_rate: Gauge
    request_duration_seconds: Histogram
    inventory_check_duration_seconds: Histogram
    requests_total: Counter


def create_order_metrics(registry: CollectorRegistry = REGISTRY) -> OrderMetrics:
    """Register the order-stub metric family on ``registry`` and return it."""
    return OrderMetrics(
        processing_failure_rate=Gauge(
            "order_processing_failure_rate",
            "Fraction of orders currently failing (0.0-1.0).",
            registry=registry,
        ),
        checkout_timeout_rate=Gauge(
            "checkout_timeout_rate",
            "Fraction of checkouts currently timing out (0.0-1.0).",
            registry=registry,
        ),
        request_duration_seconds=Histogram(
            "order_request_duration_seconds",
            "Order request duration in seconds.",
            registry=registry,
        ),
        inventory_check_duration_seconds=Histogram(
            "inventory_check_duration_seconds",
            "Inventory check duration in seconds.",
            registry=registry,
        ),
        requests_total=Counter(
            "order_requests_total",
            "Total order requests handled.",
            registry=registry,
        ),
    )
