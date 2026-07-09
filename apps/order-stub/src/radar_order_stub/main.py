"""order-stub service assembly.

Wires the five metrics and the chaos controller into a small FastAPI app:
``/healthz``, ``/metrics``, and the three ``/chaos/*`` endpoints. No ``/readyz``,
no agent token, no ``POST /events`` — this is a POC target, not a RADAR service
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

from radar_order_stub.chaos import ChaosController, ChaosRequest
from radar_order_stub.metrics import create_order_metrics

SERVICE_NAME = "order-stub"


def create_app(*, metrics_registry: CollectorRegistry = REGISTRY) -> FastAPI:
    """Build the order-stub app. ``metrics_registry`` is injectable so a fresh
    ``CollectorRegistry`` avoids duplicate-registration across app instances."""
    configure_logging(service_name=SERVICE_NAME)
    log = get_logger(SERVICE_NAME)

    metrics = create_order_metrics(metrics_registry)
    chaos = ChaosController()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("order_stub.started")
        try:
            yield
        finally:
            log.info("order_stub.stopped")

    app = FastAPI(title="radar-order-stub", lifespan=lifespan)

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

    @app.post("/chaos/reset")
    async def chaos_reset() -> dict[str, str]:
        chaos.reset()
        log.info("chaos.reset")
        return {"status": "reset"}

    return app


app = create_app()
