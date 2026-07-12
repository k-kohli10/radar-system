"""Inbound alert API: source-routed ingestion endpoints.

Three POST endpoints, one per source (``prometheus``, ``kibana``, ``mock``),
all funnel into one ingestion flow. This is the RADAR entry point, not an agent
surface: the endpoints carry no ``X-Radar-Agent-Token`` and there is no
``POST /events`` here. Inbound authentication uses a per-source
``X-Radar-Webhook-Token`` (added with the webhook-auth commit; see ADR 0011).

Each request carries exactly one alert (ADR 0011). The handler binds a
correlation id, normalizes the vendor payload to a ``NormalizedAlert`` (a
malformed or batched payload is rejected 422, never crashes), then persists it
in one transaction: it either attaches to an open incident with the same
fingerprint (no outbox event) or opens a new incident and publishes an
``alert.normalized`` outbox event — all-or-nothing. The response is 202 with the
``incident_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from radar_common import (
    InvalidPayloadError,
    bind_correlation_id,
    get_logger,
    new_correlation_id,
)
from radar_database import Database

from radar_ingestion.normalizer import AlertSource, normalize
from radar_ingestion.publisher import persist_alert
from radar_ingestion.security import WebhookAuth

log = get_logger("ingestion.routes")


def create_alerts_router(
    *,
    get_database: Callable[[], Database | None],
    webhook_auth: WebhookAuth,
) -> APIRouter:
    """Build the ``/alerts/{prometheus,kibana,mock}`` routing surface.

    ``get_database`` returns the live :class:`~radar_database.Database` (set at
    startup) or ``None`` if the service is not ready — the handler answers 503
    in that case rather than touching a missing database. Each endpoint is
    guarded by ``webhook_auth`` for its own source's ``X-Radar-Webhook-Token``.
    """
    router = APIRouter()

    async def _ingest(
        source: AlertSource, payload: dict[str, Any]
    ) -> dict[str, str | bool]:
        # One correlation id per inbound alert, bound so every downstream log
        # line, the incident, the alert, and the outbox event all share it.
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

        database = get_database()
        if database is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ingestion is not ready",
            )

        # dedup query, incident/alert insert, and outbox write are one
        # transaction: the caller-owned commit makes them all-or-nothing.
        async with database.session() as session:
            result = await persist_alert(session, alert, as_of=alert.received_at)
            await session.commit()

        log.info(
            "alert.persisted",
            source=source.value,
            service_name=alert.service_name,
            alert_name=alert.alert_name,
            severity=alert.severity.value,
            fingerprint=alert.fingerprint,
            incident_id=str(result.incident_id),
            deduplicated=result.deduplicated,
        )
        return {
            "status": "accepted",
            "correlation_id": str(correlation_id),
            "incident_id": str(result.incident_id),
            "deduplicated": result.deduplicated,
        }

    @router.post(
        "/alerts/prometheus",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(webhook_auth.require(AlertSource.PROMETHEUS))],
    )
    async def ingest_prometheus(payload: dict[str, Any]) -> dict[str, str | bool]:
        return await _ingest(AlertSource.PROMETHEUS, payload)

    @router.post(
        "/alerts/kibana",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(webhook_auth.require(AlertSource.KIBANA))],
    )
    async def ingest_kibana(payload: dict[str, Any]) -> dict[str, str | bool]:
        return await _ingest(AlertSource.KIBANA, payload)

    @router.post(
        "/alerts/mock",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(webhook_auth.require(AlertSource.MOCK))],
    )
    async def ingest_mock(payload: dict[str, Any]) -> dict[str, str | bool]:
        return await _ingest(AlertSource.MOCK, payload)

    return router
