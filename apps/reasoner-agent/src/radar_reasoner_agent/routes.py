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
from pydantic import ValidationError
from radar_common import EventsAuth, bind_correlation_id, get_logger
from radar_contracts import EventEnvelope, ReasoningRequestedPayload
from radar_database import Database, is_already_processed, mark_processed

from radar_reasoner_agent.config import SERVICE_NAME
from radar_reasoner_agent.context import ContextNotAvailableError, build_context_bundle
from radar_reasoner_agent.fallback import resolve
from radar_reasoner_agent.llm import GatewayClient
from radar_reasoner_agent.storage import store_recommendation

log = get_logger("reasoner.routes")

REASONING_REQUESTED_EVENT = "incident.reasoning_requested"
"""The only event type the reasoner consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    get_gateway: Callable[[], GatewayClient | None],
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
        gateway = get_gateway()
        if database is None or gateway is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="reasoner-agent is not ready",
            )

        # ---- 1 & 2: the gate, then the read. Nothing is written here. ----
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

            payload = _parse_payload(envelope)

            try:
                bundle = await build_context_bundle(
                    session,
                    incident_id=payload.incident_id,
                    plan_id=payload.plan_id,
                )
            except ContextNotAvailableError as exc:
                # A missing incident, a missing plan, or — the one that matters — a
                # plan belonging to a DIFFERENT incident than the event claims.
                # Reasoning over a mismatched pair would produce an RCA about one
                # incident using another's checklist, so it is refused rather than
                # skipped: 422 (permanent -> dead-letter -> a human), and NO marker,
                # because nothing was decided and the event must not look handled.
                log.error(
                    "context.unavailable",
                    event_id=str(envelope.event_id),
                    detail=str(exc),
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc

            log.info(
                "context.built",
                incident_id=str(bundle.incident_id),
                severity=bundle.severity.value,
                alert_count=bundle.alert_count,
                steps=len(bundle.investigation_steps),
            )

        # ---- 3: the LLM call. NO TRANSACTION IS OPEN. ----
        #
        # A minute-long call to a third party must not hold a database connection and
        # a row lock. The transaction above is closed; the one below has not started.
        #
        # `complete` does not raise (it returns a typed LLMFailure), and neither does
        # the parser inside `resolve`. So there is no exception path between here and
        # the write — and `resolve` is total over the result space. An incident that
        # reaches this line WILL have a recommendation to store.
        llm_result = await gateway.complete(bundle)
        outcome = resolve(bundle, llm_result)

        # ---- 4: the write, atomically. ----
        #
        # A crash before this point leaves no marker and no recommendation, so the
        # redelivery does the work again — at the cost of one repeated LLM call, which
        # is the price of not holding a transaction across a call to OpenAI.
        async with database.session() as session:
            # ONE transaction: the recommendation, the recommendation.created outbox
            # event, the audit row, and the marker. The marker lands WITH the
            # recommendation or not at all — a marker that committed first would leave
            # a redelivery no-op'ing over an incident that has no RCA.
            recommendation_id = await store_recommendation(
                session,
                correlation_id=envelope.correlation_id,
                incident_id=payload.incident_id,
                plan_id=payload.plan_id,
                outcome=outcome,
            )
            await mark_processed(session, envelope.event_id, SERVICE_NAME)
            await session.commit()

        log.info(
            "recommendation.created",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            incident_id=str(payload.incident_id),
            recommendation_id=str(recommendation_id),
            is_fallback=outcome.is_fallback,
            confidence=outcome.confidence.value,
            llm_provider=outcome.llm_provider,
        )
        return {"status": "processed"}

    return router


def _parse_payload(envelope: EventEnvelope) -> ReasoningRequestedPayload:
    """Validate the event body against the shape the planner promised to send.

    The planner *constructs* this same model, so a mismatch is a real malformation
    rather than a difference of opinion: 422, which the worker treats as permanent and
    dead-letters rather than retrying a body that will never parse.
    """
    try:
        return ReasoningRequestedPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        log.warning(
            "event.malformed_payload",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"malformed {envelope.event_type} payload: "
            f"{exc.error_count()} field error(s)",
        ) from exc
