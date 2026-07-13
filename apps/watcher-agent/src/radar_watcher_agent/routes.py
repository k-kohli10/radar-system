"""The ``POST /events`` endpoint: idempotency first, then work.

The outbox worker delivers at *least* once — a dispatch that times out, or a worker
that dies after delivering but before recording the result, is redelivered. So the
first thing this handler does, before any interpretation of the event at all, is ask
``processed_events`` whether this ``event_id`` has already been handled by
``watcher-agent``. If it has, the answer is 200 and nothing else happens. That check
is not an optimization; it is what makes at-least-once delivery safe.

It lands in this commit — before there is any correlation logic to protect — on
purpose. Idempotency added after the fact is idempotency that some path forgot: the
gate has to be the shape of the handler, not a line inside it.

Everything the event changes is in the SAME transaction as the marker:

    processed_events row  +  incident.plan_requested outbox event  +  audit_log row

Committed together, so a crash between them is impossible. Redelivery after a
successful commit finds the marker and no-ops; redelivery after a rollback finds
nothing and does the work — exactly once, either way. The marker's ``(event_id,
processed_by)`` primary key is the backstop: two concurrent deliveries that both
pass the check race to insert, and the loser gets an IntegrityError rather than
doing the work twice.

Splitting that boundary would be quietly catastrophic rather than merely wrong. A
marker committed without its event means the incident is never planned — and the gate
then ensures it is never planned on redelivery *either*, because the marker says the
work was done. The pipeline would stop for that incident, permanently and silently.

Suppression and escalation join the same transaction in the next commit.

Status codes are the documented agent contract: 200 processed (or already seen),
401 bad token, 422 malformed payload — and 401 beats 422 (see ``security``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from radar_common import bind_correlation_id, get_logger
from radar_contracts import AlertNormalizedPayload, EventEnvelope
from radar_database import Database, is_already_processed, mark_processed

from radar_watcher_agent.config import SERVICE_NAME
from radar_watcher_agent.correlation import IncidentNotFoundError, correlate
from radar_watcher_agent.security import EventsAuth

log = get_logger("watcher.routes")

ALERT_NORMALIZED_EVENT = "alert.normalized"
"""The only event type the watcher consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    events_auth: EventsAuth,
) -> APIRouter:
    """Build the ``POST /events`` surface.

    ``get_database`` returns the live :class:`~radar_database.Database` (set during
    startup) or ``None`` when the service is not ready — the handler answers 503
    then, rather than touching a database that is not there.
    """
    router = APIRouter()

    @router.post("/events", dependencies=[Depends(events_auth.require())])
    async def receive_event(envelope: EventEnvelope) -> dict[str, str]:
        # Bind before anything else can log: every line this request produces —
        # including a rejection — carries the correlation id minted at ingress, so
        # the whole pipeline is traceable by that value alone (Phase 10 depends on
        # this being true of every log line, not just the happy path).
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        if database is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="watcher-agent is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all: a redelivery must not be able to reach any work, whatever the
            # work later becomes.
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
                # mean this incident is never planned — and, because the gate would
                # then skip the redelivery, never planned on any retry either. The
                # failure would be permanent and silent. Hence one commit, not two.
                outcome = await correlate(
                    session,
                    correlation_id=envelope.correlation_id,
                    payload=payload,
                )
            except IncidentNotFoundError as exc:
                # Should be unreachable — ingestion writes the incident and the event
                # in one transaction. 422 (permanent → dead-letter) rather than a
                # retry, so a human sees it instead of the worker hammering a row that
                # is not coming back. No marker is written: nothing was decided.
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
        )
        return {"status": "processed"}

    return router


def _parse_payload(envelope: EventEnvelope) -> AlertNormalizedPayload:
    """Validate the event body against the shape ingestion promised to send.

    The envelope's ``payload`` is an open dict by design — the envelope is generic
    transport — so the per-event-type shape is judged here, by the agent that knows
    what its own events mean. Ingestion *constructs* this same model, so a mismatch is
    not a difference of opinion about the format: it is a real malformation, and a 422
    is the honest answer (the worker treats it as permanent and dead-letters it, rather
    than retrying a body that will never parse).
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
