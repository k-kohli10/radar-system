"""The ``POST /events`` endpoint: idempotency first, then work.

The outbox worker delivers at *least* once — a dispatch that times out, or a
worker that dies after delivering but before recording the result, is
redelivered. So the first thing this handler does, before any interpretation of
the event at all, is ask ``processed_events`` whether this ``event_id`` has
already been handled by ``planner-agent``. If it has, the answer is 200 and
nothing else happens.

It lands in this commit — before there is any planning logic to protect — on
purpose. Idempotency added after the fact is idempotency that some path forgot:
the gate has to be the shape of the handler, not a line inside it.

Everything the event changes will be in the SAME transaction as the marker:

    processed_events  +  investigation_plan  +  reasoning_requested outbox event

Committed together, so a crash between them is impossible. A marker committed
without its plan would be quietly terminal: the incident would never be planned,
and the gate would then ensure it was never planned on redelivery *either*,
because the marker says the work was done. The pipeline would stop for that
incident, permanently and silently.

Template matching and the transactional write land in the next two commits,
inside that same transaction.

Status codes are the documented agent contract: 200 processed (or already seen),
401 bad token, 422 malformed payload — and 401 beats 422 (the shared guard in
``radar_common.auth``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from radar_common import EventsAuth, bind_correlation_id, get_logger
from radar_contracts import EventEnvelope
from radar_database import Database, is_already_processed, mark_processed

from radar_planner_agent.config import SERVICE_NAME

log = get_logger("planner.routes")

PLAN_REQUESTED_EVENT = "incident.plan_requested"
"""The only event type the planner consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    events_auth: EventsAuth,
) -> APIRouter:
    """Build the ``POST /events`` surface.

    ``get_database`` returns the live :class:`~radar_database.Database` (set
    during startup) or ``None`` when the service is not ready — the handler
    answers 503 then, rather than touching a database that is not there.
    """
    router = APIRouter()

    @router.post("/events", dependencies=[Depends(events_auth.require())])
    async def receive_event(envelope: EventEnvelope) -> dict[str, str]:
        # Bind before anything else can log: every line this request produces,
        # including a rejection, carries the correlation id minted at ingress, so
        # the pipeline stays traceable by that value alone.
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        if database is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="planner-agent is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all: a redelivery must not be able to reach any work, whatever
            # the work later becomes.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != PLAN_REQUESTED_EVENT:
                # A type this agent does not handle is not an error to retry: the
                # worker would redeliver it forever. Mark it seen and drop it, so
                # it is dispatched exactly once and then never again.
                log.warning(
                    "event.unhandled_type",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
                return {"status": "ignored"}

            # Template matching, the investigation_plan insert, and the
            # reasoning_requested outbox write land HERE, in this transaction.
            await mark_processed(session, envelope.event_id, SERVICE_NAME)
            await session.commit()

        log.info(
            "event.processed",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        return {"status": "processed"}

    return router
