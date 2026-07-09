"""Inbound alert API: source-routed ingestion endpoints.

Three POST endpoints, one per source (``prometheus``, ``kibana``, ``mock``),
all funnel into one ingestion flow. This is the RADAR entry point, not an agent
surface: the endpoints carry no ``X-Radar-Agent-Token`` and there is no
``POST /events`` here. Inbound authentication uses a per-source
``X-Radar-Webhook-Token`` (added with the webhook-auth commit; see ADR 0011).

Each request carries exactly one alert (ADR 0011): the handler binds a
correlation id and normalizes the vendor payload to a ``NormalizedAlert`` — a
malformed or batched payload is rejected with 422, never crashes. Once the
following commits land, the handler also deduplicates by fingerprint and
transactionally opens an incident, at which point the ``incident_id`` joins the
202 response.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from radar_common import (
    InvalidPayloadError,
    bind_correlation_id,
    get_logger,
    new_correlation_id,
)

from radar_ingestion.normalizer import AlertSource, normalize

log = get_logger("ingestion.routes")


def create_alerts_router() -> APIRouter:
    """Build the ``/alerts/{prometheus,kibana,mock}`` routing surface."""
    router = APIRouter()

    async def _ingest(source: AlertSource, payload: dict[str, Any]) -> dict[str, str]:
        # One correlation id per inbound alert, bound so every downstream log
        # line (and, later, the incident and outbox event) shares it.
        correlation_id = new_correlation_id()
        bind_correlation_id(correlation_id)
        try:
            alert = normalize(source, payload, correlation_id=correlation_id)
        except InvalidPayloadError as exc:
            # Malformed or batched vendor payload: a real 422, never a crash.
            log.warning("alert.rejected", source=source.value, reason=str(exc))
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        log.info(
            "alert.normalized",
            source=source.value,
            service_name=alert.service_name,
            alert_name=alert.alert_name,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
        )
        # deduplicate -> transactional persist + outbox publish arrive in the
        # following commits; the incident_id joins the response once persistence
        # lands.
        return {
            "status": "accepted",
            "correlation_id": str(correlation_id),
            "fingerprint": alert.fingerprint,
        }

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
