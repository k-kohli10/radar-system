"""What the watcher decides about an incident, and the event it emits.

Ingestion has already resolved which incident the alert landed on. What is left is the
judgement the watcher exists to make: **does this incident need an investigation?**

    new incident        -> emit incident.plan_requested  (the pipeline starts)
    deduplicated alert  -> emit nothing                  (it is already planning)

The dedup branch emitting a second ``plan_requested`` would be a real bug, not a
harmless duplicate: the planner would build a second investigation plan for an
incident already downstream, the reasoner would burn a second LLM call on it, and the
engineer would get two RCA cards for one incident. The branch is on
``payload.deduplicated`` — a fact ingestion decided and stated, not something the
watcher re-derives — so the two cannot disagree about what "new" means.

Escalation lands on the dedup branch in the next commit; it is the reason duplicates
are dispatched here at all.

EVERYTHING IN ONE TRANSACTION
-----------------------------
The caller opens the session and commits once. The ``plan_requested`` outbox event,
the ``audit_log`` row, and the ``processed_events`` marker are therefore all-or-nothing
— the same rule as ingestion's incident/alert/outbox trio. A marker that committed
without its event would mean the incident silently never gets planned, and the
idempotency gate would ensure it never gets planned on redelivery either: the failure
would be permanent and completely silent. That is the failure this boundary exists to
make impossible.

CORRELATION ID
--------------
Every row written here carries the correlation id from the *envelope* — the value
minted at ingress and threaded through ingestion's incident, its alert, and the
``alert.normalized`` event that arrived. It is never re-minted. Phase 10's "trace the
whole pipeline by correlation_id alone" is only true if every stage passes the value
through rather than generating its own, and the only way that stays true is for the
tests to anchor on the original ingress UUID and assert equality — which they do.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from radar_common import RadarError, get_logger
from radar_contracts import AlertNormalizedPayload, PlanRequestedPayload, Severity
from radar_database import AuditLog, Incident, write_outbox_event
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger("watcher.correlation")

PLAN_REQUESTED_EVENT = "incident.plan_requested"
"""Outbox event type the watcher emits to start an investigation."""

PLANNER_TARGET = "planner-agent"
"""Target service for the ``incident.plan_requested`` event."""

AUDIT_PLAN_REQUESTED = "watcher.plan_requested"
AUDIT_ALERT_ATTACHED = "watcher.alert_attached"


class IncidentNotFoundError(RadarError):
    """The incident named by the event payload does not exist.

    This should be impossible: ingestion writes the incident and the
    ``alert.normalized`` event in the SAME transaction, so an event that exists
    implies an incident that exists. If it happens, something deleted the incident
    out from under the pipeline, and the honest response is to fail loudly rather
    than invent one — the route maps it to 422, which the outbox worker treats as
    permanent and dead-letters, so it surfaces to a human instead of being retried
    forever against a row that is not coming back.
    """


@dataclass(frozen=True, slots=True)
class CorrelationOutcome:
    """What the watcher decided about one alert."""

    incident_id: UUID
    plan_requested: bool


async def correlate(
    session: AsyncSession,
    *,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> CorrelationOutcome:
    """Decide what this alert means for its incident, and write the consequences.

    Adds rows but does NOT commit — the caller owns the transaction boundary, so the
    outbox event, the audit row, and the caller's ``processed_events`` marker are one
    atomic unit.
    """
    incident = await session.get(Incident, payload.incident_id)
    if incident is None:
        raise IncidentNotFoundError(
            f"incident {payload.incident_id} does not exist; ingestion writes the "
            "incident and the event in one transaction, so this row was deleted"
        )

    if payload.deduplicated:
        # Already planning. Emitting a second plan_requested here would re-plan an
        # incident that is already downstream — a duplicate RCA, and a second LLM
        # call, for one incident. Escalation (next commit) is what this branch is for.
        session.add(
            _audit(
                AUDIT_ALERT_ATTACHED,
                incident=incident,
                correlation_id=correlation_id,
                payload=payload,
            )
        )
        log.info(
            "alert.attached",
            incident_id=str(incident.id),
            service_name=payload.service_name,
            alert_name=payload.alert_name,
            alert_count=incident.alert_count,
        )
        return CorrelationOutcome(incident_id=incident.id, plan_requested=False)

    await _request_plan(
        session, incident=incident, correlation_id=correlation_id, payload=payload
    )
    session.add(
        _audit(
            AUDIT_PLAN_REQUESTED,
            incident=incident,
            correlation_id=correlation_id,
            payload=payload,
        )
    )
    log.info(
        "incident.plan_requested",
        incident_id=str(incident.id),
        service_name=payload.service_name,
        alert_name=payload.alert_name,
        severity=incident.severity,
    )
    return CorrelationOutcome(incident_id=incident.id, plan_requested=True)


async def _request_plan(
    session: AsyncSession,
    *,
    incident: Incident,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> None:
    """Add the ``incident.plan_requested`` outbox event (no commit).

    ``severity`` is read from the INCIDENT, not the alert: escalation may have raised
    it, and the planner and the engineer should see the incident's severity now, not
    the severity of whichever alert happened to trigger the request.

    ``alert_name`` comes from the alert, because the ``incidents`` table has no such
    column — and the planner matches its template on ``service_name:alert_name``.
    """
    body = PlanRequestedPayload(
        incident_id=incident.id,
        service_name=incident.service_name,
        alert_name=payload.alert_name,
        severity=Severity(incident.severity),
        alert_count=incident.alert_count,
    )
    await write_outbox_event(
        session,
        event_type=PLAN_REQUESTED_EVENT,
        target_service=PLANNER_TARGET,
        payload=body.model_dump(mode="json"),
        # The ingress value, passed through — never a fresh UUID. This is the link
        # in the chain Phase 10 traces by.
        correlation_id=correlation_id,
    )


def _audit(
    event_type: str,
    *,
    incident: Incident,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> AuditLog:
    """Build the append-only audit record for what the watcher just decided."""
    return AuditLog(
        event_type=event_type,
        entity_type="incident",
        entity_id=incident.id,
        correlation_id=correlation_id,
        actor="watcher-agent",
        payload={
            "alert_id": str(payload.id),
            "service_name": payload.service_name,
            "alert_name": payload.alert_name,
            "severity": incident.severity,
            "alert_count": incident.alert_count,
            "deduplicated": payload.deduplicated,
        },
    )
