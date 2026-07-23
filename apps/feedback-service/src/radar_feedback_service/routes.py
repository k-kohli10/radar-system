"""The ``POST /events`` endpoint: idempotency first, then the delivery.

The outbox worker delivers at *least* once, so the first thing this handler does —
before any interpretation of the event at all — is ask ``processed_events`` whether
this ``event_id`` has already been handled by ``feedback-service``. If it has, the
answer is 200 and nothing else happens.

The gate matters here for a reason unique to this service: the work it guards is a
message to a HUMAN. A duplicate does not cost a wasted transaction or an LLM call —
it puts a second RCA card in the on-call engineer's channel for one incident, and
they cannot tell which is current.

SCOPE OF THIS COMMIT
--------------------
This is the service skeleton: it exists, it authenticates, it gates, and it is
REACHABLE by the outbox worker. Actually delivering the Slack card is the next
commits' work.

So ``recommendation.created`` — this service's own event — is answered **503, and
deliberately NOT marked processed**. That combination is the whole point:

- **Not marked processed** because marking it would consume the event forever. The
  established pattern for an event a service does not handle is mark-and-drop
  (see the reasoner's ``event.unhandled_type``), and applying it here would be
  silent data loss of the worst kind: the RCA card for a real incident, dropped
  before the code that delivers it was ever written, with a ``processed_events``
  row asserting it was handled.
- **503, not 422**, because 503 is retryable and 422 is permanent. The event stays
  in the outbox and is redelivered, so once the delivery handler lands it is
  delivered for real. If retries exhaust first the event dead-letters, which is
  recoverable by replay (ADR 0017) — where a 422 would dead-letter it immediately.

An event type belonging to a DIFFERENT service is a different case and does take
the standard mark-and-drop: it is not ours, nobody else will deliver it, and an
error would have the worker retry it forever.

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

from radar_feedback_service.config import SERVICE_NAME

log = get_logger("feedback.routes")

RECOMMENDATION_CREATED_EVENT = "recommendation.created"
"""The event this service consumes: the reasoner has written an RCA to deliver."""


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
    async def handle_event(envelope: EventEnvelope) -> dict[str, str]:
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        if database is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="feedback-service is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all. A redelivery must not be able to reach any work — and here
            # "work" means a second RCA card in a human's channel.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != RECOMMENDATION_CREATED_EVENT:
                # Not this service's event. An error would have the worker retry it
                # forever; marked seen and dropped, it is delivered exactly once.
                log.warning(
                    "event.unhandled_type",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
                return {"status": "ignored"}

        # Ours, but the delivery handler does not exist yet. NO marker is written
        # and nothing is committed: the event must stay deliverable. 503 is
        # retryable, so the worker keeps it rather than dead-lettering it.
        log.warning(
            "event.delivery_not_implemented",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="rca delivery is not implemented yet; event left for redelivery",
        )

    return router
