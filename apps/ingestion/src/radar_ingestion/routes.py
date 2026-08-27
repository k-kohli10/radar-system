"""Inbound alert API: source-routed ingestion endpoints.

Three POST endpoints, one per source (``prometheus``, ``kibana``, ``mock``), all
funnelling into one flow. This is the RADAR entry point, not an agent surface: no
``X-Radar-Agent-Token``, no ``POST /events``. Inbound auth is a per-source
``X-Radar-Webhook-Token`` (ADR 0011).

Each request carries exactly one alert (ADR 0011). The handler binds a correlation
id, normalizes the vendor payload (a malformed or batched payload is a 422, never a
crash), then persists it in one transaction. Every response is 202; the body's
``status`` says what happened:

- ``accepted`` — a firing alert opened or attached to an incident (``incident_id``,
  ``deduplicated``), publishing an ``alert.normalized`` event.
- ``resolved`` — a resolve matched a live incident and flipped its firing alert rows
  (``incident_id``, ``alerts_resolved``; 0 on a duplicate delivery).
- ``ignored`` — a resolve matched no live incident: nothing opened, nothing
  published, only an ``audit_log`` receipt. No incident id, because there is none.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from radar_common import (
    InvalidPayloadError,
    get_logger,
    new_correlation_id,
)
from radar_database import Database
from radar_telemetry import IngestionMetrics, bind_correlation_id

from radar_ingestion.normalizer import AlertSource, normalize
from radar_ingestion.publisher import persist_alert
from radar_ingestion.security import WebhookAuth

log = get_logger("ingestion.routes")


def create_alerts_router(
    *,
    get_database: Callable[[], Database | None],
    webhook_auth: WebhookAuth,
    metrics: IngestionMetrics,
) -> APIRouter:
    """Build the ``/alerts/{prometheus,kibana,mock}`` routing surface.

    ``get_database`` returns the live :class:`~radar_database.Database` (set at
    startup) or ``None`` if the service is not ready — the handler answers 503
    in that case rather than touching a missing database. Each endpoint is
    guarded by ``webhook_auth`` for its own source's ``X-Radar-Webhook-Token``.

    ``metrics`` carries ``incidents_total``, ticked once per incident this route
    OPENS (not per alert): dedup attaches and resolves do not move it.
    """
    router = APIRouter()

    async def _ingest(
        source: AlertSource, payload: dict[str, Any]
    ) -> dict[str, str | bool | int]:
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

        if result.ignored:
            # Resolved alert with no live incident: the audit_log row persist_alert
            # wrote is the only trace, and there is no incident id to return.
            log.info(
                "alert.resolve_ignored",
                source=source.value,
                service_name=alert.service_name,
                alert_name=alert.alert_name,
                fingerprint=alert.fingerprint,
            )
            return {
                "status": "ignored",
                "correlation_id": str(correlation_id),
                "reason": "no_open_incident_for_resolved_alert",
            }

        # A firing alert or a matched resolve always lands on an incident. A `raise`,
        # not an `assert`: `assert` is stripped under `python -O`, which would let a
        # None id flow silently into the response.
        if result.incident_id is None:
            raise RuntimeError(
                "persist_alert returned a non-ignored result with no incident_id; "
                "a firing or matched alert must always land on an incident"
            )

        if result.alerts_resolved is not None:
            # A resolve that matched a live incident.
            log.info(
                "alert.resolved",
                source=source.value,
                service_name=alert.service_name,
                alert_name=alert.alert_name,
                fingerprint=alert.fingerprint,
                incident_id=str(result.incident_id),
                alerts_resolved=result.alerts_resolved,
                incident_resolved=result.incident_resolved,
            )
            return {
                "status": "resolved",
                "correlation_id": str(correlation_id),
                "incident_id": str(result.incident_id),
                "alerts_resolved": result.alerts_resolved,
                "incident_resolved": result.incident_resolved,
            }

        if not result.deduplicated:
            # A NEW incident was opened. Counted AFTER the commit above: incrementing
            # before it would count opens a rolled-back transaction never made
            # durable.
            metrics.incidents_total.labels(
                alert.service_name, alert.severity.value
            ).inc()

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
    async def ingest_prometheus(payload: dict[str, Any]) -> dict[str, str | bool | int]:
        return await _ingest(AlertSource.PROMETHEUS, payload)

    @router.post(
        "/alerts/kibana",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(webhook_auth.require(AlertSource.KIBANA))],
    )
    async def ingest_kibana(payload: dict[str, Any]) -> dict[str, str | bool | int]:
        return await _ingest(AlertSource.KIBANA, payload)

    @router.post(
        "/alerts/mock",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(webhook_auth.require(AlertSource.MOCK))],
    )
    async def ingest_mock(payload: dict[str, Any]) -> dict[str, str | bool | int]:
        return await _ingest(AlertSource.MOCK, payload)

    return router
