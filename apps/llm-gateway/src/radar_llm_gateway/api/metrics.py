"""The ``/metrics`` route: Prometheus text format.

Renders the metric families the gateway registers at startup (request,
LLM, and any others sharing the registry) via the shared
``radar_telemetry.render_latest``. Unauthenticated by design — Prometheus
scrapes it — and it exposes only metric names, labels, and numbers; nothing
request-derived beyond the bounded label values.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import REGISTRY, CollectorRegistry
from radar_telemetry import render_latest


def create_metrics_router(registry: CollectorRegistry = REGISTRY) -> APIRouter:
    """Build the ``/metrics`` route over ``registry`` (injectable for tests)."""
    router = APIRouter()

    @router.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = render_latest(registry)
        return Response(content=payload, media_type=content_type)

    return router
