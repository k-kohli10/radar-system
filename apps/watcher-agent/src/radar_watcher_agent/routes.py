"""The ``POST /events`` endpoint: idempotency first, then work.

The outbox worker delivers at *least* once: a dispatch that times out, or a worker that
dies after delivering but before recording the result, is redelivered. So before any
interpretation of the event at all, this handler asks ``processed_events`` whether this
``event_id`` has already been handled by ``watcher-agent``. If it has, the answer is 200
and nothing else happens. That check is what makes at-least-once delivery safe.

Everything the event changes is in the SAME transaction as the marker:

    processed_events row  +  incident.plan_requested outbox event  +  audit_log row

Redelivery after a successful commit finds the marker and no-ops; redelivery after a
rollback finds nothing and does the work. The marker's ``(event_id, processed_by)``
primary key is the backstop: two concurrent deliveries that both pass the check race
to insert, and the loser gets an IntegrityError rather than doing the work twice.

Splitting that boundary would be quietly catastrophic. A marker committed without its
event means the incident is never planned, and the gate then skips the redelivery
because the marker says the work was done: the pipeline stops for that incident,
permanently and silently.

Status codes are the documented agent contract: 200 processed (or already seen),
401 bad token, 422 malformed payload. 401 beats 422 (see ``security``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from radar_common import EventsAuth, get_logger
from radar_contracts import AlertNormalizedPayload, EventEnvelope
from radar_database import Database, is_already_processed, mark_processed
from radar_telemetry import bind_correlation_id

from radar_watcher_agent.config import SERVICE_NAME
from radar_watcher_agent.correlation import IncidentNotFoundError, correlate
from radar_watcher_agent.rules import CorrelationRules

log = get_logger("watcher.routes")

ALERT_NORMALIZED_EVENT = "alert.normalized"
"""The only event type the watcher consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    get_rules: Callable[[], CorrelationRules | None],
    events_auth: EventsAuth,
) -> APIRouter:
    """Build the ``POST /events`` surface.

    ``get_database`` returns the live :class:`~radar_database.Database` (set during
    startup) or ``None`` when the service is not ready, in which case the handler
    answers 503 rather than touching a database that is not there.
    """
    router = APIRouter()

    @router.post("/events", dependencies=[Depends(events_auth.require())])
    async def receive_event(envelope: EventEnvelope) -> dict[str, str]:
        # Bind before anything else can log, so every line this request produces
        # (rejections included) carries the correlation id minted at ingress. Phase 10
        # traces by that value alone.
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        rules = get_rules()
        if database is None or rules is None:
            # A watcher whose ConfigMap did not load refuses the work rather than
            # inventing policy. 503 is retryable: the worker backs off and redelivers.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="watcher-agent is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted at
            # all, so a redelivery cannot reach any work.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != ALERT_NORMALIZED_EVENT:
                # A type this agent does not handle is not an error to retry: the
                # worker would redeliver it forever. Mark it seen and drop it, so it
                # is dispatched exactly once and then never again.
                log.warning(
                    "event.unhandled_type",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
                return {"status": "ignored"}

            payload = _parse_payload(envelope)

            try:
                # The correlation decision and the idempotency marker are written in
                # ONE transaction, committed below. A marker without its event would
                # mean this incident is never planned, on this delivery or any retry.
                outcome = await correlate(
                    session,
                    rules=rules,
                    correlation_id=envelope.correlation_id,
                    payload=payload,
                )
            except IncidentNotFoundError as exc:
                # 422 (permanent, dead-lettered) rather than a retry, so a human sees
                # it instead of the worker hammering a row that is not coming back.
                # No marker is written: nothing was decided.
                log.error("incident.not_found", event_id=str(envelope.event_id))
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc

            await mark_processed(session, envelope.event_id, SERVICE_NAME)
            await session.commit()

        log.info(
            "event.processed",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            incident_id=str(outcome.incident_id),
            plan_requested=outcome.plan_requested,
            suppressed=outcome.suppressed,
            escalated_to=outcome.escalated_to.value if outcome.escalated_to else None,
        )
        return {"status": "processed"}

    return router


def _parse_payload(envelope: EventEnvelope) -> AlertNormalizedPayload:
    """Validate the event body against the shape ingestion promised to send.

    The envelope is generic transport and its ``payload`` an open dict, so the
    per-event-type shape is judged here, by the agent that knows what its own events
    mean. Ingestion constructs this same model, so a mismatch is a real malformation:
    422, which the worker treats as permanent and dead-letters rather than retrying a
    body that will never parse.
    """
    try:
        return AlertNormalizedPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        log.warning(
            "event.malformed_payload",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"malformed {envelope.event_type} payload: {exc.error_count()} "
            "field error(s)",
        ) from exc
