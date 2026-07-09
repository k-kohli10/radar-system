"""Inbound alert API: source-routed ingestion endpoints.

Three POST endpoints, one per source (``prometheus``, ``kibana``, ``mock``),
all funnel into one ingestion flow. This is the RADAR entry point, not an agent
surface: the endpoints carry no ``X-Radar-Agent-Token`` and there is no
``POST /events`` here. Inbound authentication uses a per-source
``X-Radar-Webhook-Token`` (added with the webhook-auth commit; see ADR 0011).

This commit wires the routing and per-request correlation only. The full flow —
normalize the vendor payload to a ``NormalizedAlert``, deduplicate by
fingerprint within the window, and transactionally open an incident while
publishing the ``alert.normalized`` outbox event — is filled in by the
following commits, at which point the response carries the ``incident_id``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import APIRouter, status
from radar_common import bind_correlation_id, get_logger, new_correlation_id

log = get_logger("ingestion.routes")


class AlertSource(StrEnum):
    """The alert sources ingestion accepts, one per ``/alerts/*`` endpoint."""

    PROMETHEUS = "prometheus"
    KIBANA = "kibana"
    MOCK = "mock"


def create_alerts_router() -> APIRouter:
    """Build the ``/alerts/{prometheus,kibana,mock}`` routing surface."""
    router = APIRouter()

    async def _ingest(source: AlertSource, payload: dict[str, Any]) -> dict[str, str]:
        # One correlation id per inbound alert, bound so every downstream log
        # line (and, later, the incident and outbox event) shares it.
        correlation_id = new_correlation_id()
        bind_correlation_id(correlation_id)
        log.info("alert.received", source=source.value)
        # normalize -> deduplicate -> transactional persist + outbox publish
        # arrive in the following commits; the incident_id joins the response
        # once persistence lands.
        return {"status": "accepted", "correlation_id": str(correlation_id)}

    @router.post("/alerts/prometheus", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_prometheus(payload: dict[str, Any]) -> dict[str, str]:
        return await _ingest(AlertSource.PROMETHEUS, payload)

    @router.post("/alerts/kibana", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_kibana(payload: dict[str, Any]) -> dict[str, str]:
        return await _ingest(AlertSource.KIBANA, payload)

    @router.post("/alerts/mock", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_mock(payload: dict[str, Any]) -> dict[str, str]:
        return await _ingest(AlertSource.MOCK, payload)

    return router
