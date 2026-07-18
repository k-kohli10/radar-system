"""platform-sim service assembly.

Wires the domain metrics and the chaos controller into a small FastAPI app:
``/healthz``, ``/metrics``, and the ``/chaos/*`` endpoints. No ``/readyz``, no
agent token, no ``POST /events`` — this is a POC target, not a RADAR service
(see the package docstring).

The gauge reconciliation lives in the ``/metrics`` handler: right before
rendering, the two rate gauges are set from the chaos controller, which returns
the pinned rate while a spike's deadline is in the future and the baseline once
it has passed. That is the whole of the "compute at scrape time, auto-reset by
expiry" design — there is no background task.

Logging reuses ``radar_common.configure_logging`` (JSON to stdout). The chaos
endpoints log; ``/healthz`` and ``/metrics`` do not (they are scraped/probed
constantly and would only add noise).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import REGISTRY, CollectorRegistry
from radar_common import configure_logging, get_logger
from radar_telemetry import render_latest

from radar_platform_sim.chaos import (
    ChaosController,
    ChaosRequest,
    CounterRampRequest,
)
from radar_platform_sim.metrics import create_platform_metrics

SERVICE_NAME = "platform-sim"


def create_app(*, metrics_registry: CollectorRegistry = REGISTRY) -> FastAPI:
    """Build the platform-sim app. ``metrics_registry`` is injectable so a fresh
    ``CollectorRegistry`` avoids duplicate-registration across app instances."""
    configure_logging(service_name=SERVICE_NAME)
    log = get_logger(SERVICE_NAME)

    metrics = create_platform_metrics(metrics_registry)
    chaos = ChaosController()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("platform_sim.started")
        try:
            yield
        finally:
            log.info("platform_sim.stopped")

    app = FastAPI(title="radar-platform-sim", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def get_metrics() -> Response:
        # Reconcile the chaos-driven gauges at scrape time: the controller
        # returns the pinned rate until the spike deadline passes, then the
        # baseline. This is where auto-reset actually takes effect.
        metrics.processing_failure_rate.set(chaos.order_failure_rate())
        metrics.checkout_timeout_rate.set(chaos.checkout_timeout_rate())
        metrics.payment_gateway_error_rate.set(chaos.payment_error_rate())
        # The counter is the exception: it is advanced, not set. The drain is
        # destructive, so it must be applied here and nowhere else — dropping
        # the result would lose those declines permanently. This is also why
        # the counter only moves when something scrapes: with no scrapes there
        # is no rate() to observe anyway.
        metrics.payment_declines_total.inc(chaos.drain_payment_declines())
        payload, content_type = render_latest(metrics_registry)
        return Response(content=payload, media_type=content_type)

    @app.post("/chaos/order-failures")
    async def chaos_order_failures(req: ChaosRequest) -> dict[str, float | int | str]:
        chaos.spike_order_failures(req.rate, req.duration_seconds)
        log.info(
            "chaos.order_failures",
            rate=req.rate,
            duration_seconds=req.duration_seconds,
        )
        return {
            "status": "ok",
            "rate": req.rate,
            "duration_seconds": req.duration_seconds,
        }

    @app.post("/chaos/checkout-timeouts")
    async def chaos_checkout_timeouts(
        req: ChaosRequest,
    ) -> dict[str, float | int | str]:
        chaos.spike_checkout_timeouts(req.rate, req.duration_seconds)
        log.info(
            "chaos.checkout_timeouts",
            rate=req.rate,
            duration_seconds=req.duration_seconds,
        )
        return {
            "status": "ok",
            "rate": req.rate,
            "duration_seconds": req.duration_seconds,
        }

    @app.post("/chaos/payment-errors")
    async def chaos_payment_errors(req: ChaosRequest) -> dict[str, float | int | str]:
        chaos.spike_payment_errors(req.rate, req.duration_seconds)
        log.info(
            "chaos.payment_errors",
            rate=req.rate,
            duration_seconds=req.duration_seconds,
        )
        return {
            "status": "ok",
            "rate": req.rate,
            "duration_seconds": req.duration_seconds,
        }

    @app.post("/chaos/payment-declines")
    async def chaos_payment_declines(
        req: CounterRampRequest,
    ) -> dict[str, float | int | str]:
        chaos.ramp_payment_declines(req.per_second, req.duration_seconds)
        log.info(
            "chaos.payment_declines",
            per_second=req.per_second,
            duration_seconds=req.duration_seconds,
        )
        return {
            "status": "ok",
            "per_second": req.per_second,
            "duration_seconds": req.duration_seconds,
        }

    @app.post("/chaos/reset")
    async def chaos_reset() -> dict[str, str]:
        chaos.reset()
        log.info("chaos.reset")
        return {"status": "reset"}

    return app


app = create_app()
