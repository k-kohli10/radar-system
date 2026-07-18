"""The Prometheus metrics the platform simulator exposes.

These are e-commerce *domain* metrics for the simulated services, not the
``radar_*`` platform metrics that live in ``radar_telemetry`` — so they are
declared here, spelled exactly as the plan lists them. Rendering still reuses
``radar_telemetry.render_latest`` (see ``main.py``); nothing about metric
*output* is reimplemented.

Metric names carry no ``service`` label: one process exposes all of them, and
the simulated service a metric belongs to is attached by the alert rule that
watches it (see the package docstring). Grouping here is by naming and by the
docstring below, not by label.

Most metrics here are chaos-driven. The simulator does not simulate traffic, so
``order_requests_total`` and ``order_request_duration_seconds`` are exposed for
scraping completeness but only ever observed if a caller drives them; at rest
they render at zero.

Inventory latency is a *gauge* holding a p95, not a histogram. A histogram
cannot be pinned: ``histogram_quantile`` over ``rate(..._bucket[5m])`` needs a
stream of real observations accruing over time, which the deadline design
cannot produce, and faking it would couple the metric to scrape cadence. A
simulator asserting "p95 is currently 1.5s" is both simpler and more honest
than one faking a distribution it never uses.

Following the ``radar_telemetry`` pattern, :func:`create_platform_metrics` takes
a ``registry`` (default: the global ``REGISTRY``) and returns a frozen bundle, so
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
class PlatformMetrics:
    """The simulator's exposed metrics, grouped by simulated service.

    order-service:
        ``processing_failure_rate`` — 0.0–1.0 gauge reconciled from chaos state
        at scrape time (see ``main.py``).
        ``request_duration_seconds``, ``requests_total`` — declared for scraping
        completeness; stay at zero unless observed.

    checkout-service:
        ``checkout_timeout_rate`` — 0.0–1.0 gauge, chaos-driven.

    inventory-service:
        ``inventory_check_p95_seconds`` — chaos-driven gauge, in seconds.

    payment-gateway:
        ``payment_gateway_error_rate`` — 0.0–1.0 gauge, chaos-driven.
        ``payment_declines_total`` — counter, advanced at scrape time by the
        chaos ramp. The only metric here that evolves rather than holds.
    """

    processing_failure_rate: Gauge
    checkout_timeout_rate: Gauge
    payment_gateway_error_rate: Gauge
    payment_declines_total: Counter
    inventory_check_p95_seconds: Gauge
    request_duration_seconds: Histogram
    requests_total: Counter


def create_platform_metrics(registry: CollectorRegistry = REGISTRY) -> PlatformMetrics:
    """Register the simulator's metric family on ``registry`` and return it."""
    return PlatformMetrics(
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
        payment_gateway_error_rate=Gauge(
            "payment_gateway_error_rate",
            "Fraction of payment authorizations currently erroring (0.0-1.0).",
            registry=registry,
        ),
        payment_declines_total=Counter(
            "payment_declines",
            "Total card payments declined by the issuer.",
            registry=registry,
        ),
        inventory_check_p95_seconds=Gauge(
            "inventory_check_p95_seconds",
            "Inventory availability check p95 latency in seconds.",
            registry=registry,
        ),
        request_duration_seconds=Histogram(
            "order_request_duration_seconds",
            "Order request duration in seconds.",
            registry=registry,
        ),
        requests_total=Counter(
            "order_requests_total",
            "Total order requests handled.",
            registry=registry,
        ),
    )
