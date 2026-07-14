"""The ``POST /events`` endpoint: idempotency first, then the reasoning.

The outbox worker delivers at *least* once, so the first thing this handler does —
before any interpretation of the event at all — is ask ``processed_events`` whether
this ``event_id`` has already been handled by ``reasoner-agent``. If it has, the
answer is 200 and nothing else happens.

The gate matters more here than anywhere else in the pipeline. The watcher and the
planner write rows; a duplicate costs a wasted transaction. The reasoner calls an
LLM: a duplicate costs **money**, and produces a second root-cause analysis that
contradicts the first. It lands in this commit, before there is any reasoning to
protect, because idempotency added afterwards is idempotency some path forgot.

Everything the event changes will be in the SAME transaction as the marker:

    processed_events  +  recommendation  +  recommendation.created outbox event
                      +  audit_log

...with ONE deliberate exception: the LLM call itself happens OUTSIDE the
transaction, because it is a network call to a third party that can take a minute,
and holding a database transaction open across it would pin a connection and a row
lock on something entirely outside our control. So the shape is:

    1. gate  (inside a transaction, committed nothing yet)
    2. read the incident and plan
    3. call the LLM  ......................  no transaction open
    4. write everything, atomically  .......  one commit

A crash at step 3 leaves no marker and no recommendation, so the redelivery does the
work again — at the cost of one repeated LLM call, which is the price of not holding
a transaction across a network call to OpenAI.

Status codes are the documented agent contract: 200 processed (or already seen), 401
bad token, 422 malformed payload — and 401 beats 422 (the shared guard in
``radar_common.auth``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from radar_common import EventsAuth, bind_correlation_id, get_logger
from radar_contracts import EventEnvelope
from radar_database import Database, is_already_processed, mark_processed

from radar_reasoner_agent.config import SERVICE_NAME

log = get_logger("reasoner.routes")

REASONING_REQUESTED_EVENT = "incident.reasoning_requested"
"""The only event type the reasoner consumes."""


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
        # Bind before anything else can log: every line this request produces,
        # including a rejection, carries the correlation id minted at ingress.
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        if database is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="reasoner-agent is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all. A redelivery must not be able to reach any work — and here
            # "work" means an LLM call that costs money and produces a second,
            # contradictory RCA.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != REASONING_REQUESTED_EVENT:
                # Not this agent's event. An error would have the worker retry it
                # forever; marked seen and dropped, it is delivered exactly once.
                log.warning(
                    "event.unhandled_type",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
                return {"status": "ignored"}

            # The context bundle, the LLM call, the fallback, and the transactional
            # write land HERE over the next five commits.
            await mark_processed(session, envelope.event_id, SERVICE_NAME)
            await session.commit()

        log.info(
            "event.processed",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        return {"status": "processed"}

    return router
