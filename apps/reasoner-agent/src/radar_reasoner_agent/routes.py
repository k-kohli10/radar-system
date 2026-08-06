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
from radar_common import EventsAuth, get_logger
from radar_contracts import EventEnvelope, ReasoningRequestedPayload
from radar_database import Database, is_already_processed, mark_processed
from radar_telemetry import ReasonerMetrics, bind_correlation_id
from sqlalchemy.exc import IntegrityError

from radar_reasoner_agent.config import SERVICE_NAME
from radar_reasoner_agent.context import ContextNotAvailableError, build_context_bundle
from radar_reasoner_agent.fallback import RetrievalMetadata, resolve
from radar_reasoner_agent.knowledge import KnowledgeClient, RetrievalOutcome
from radar_reasoner_agent.llm import GatewayClient
from radar_reasoner_agent.storage import (
    IncidentNotFoundError,
    ReferencedRowMissingError,
    race_audit,
    store_recommendation,
)

log = get_logger("reasoner.routes")

REASONING_REQUESTED_EVENT = "incident.reasoning_requested"
"""The only event type the reasoner consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    get_gateway: Callable[[], GatewayClient | None],
    get_knowledge: Callable[[], KnowledgeClient | None] = lambda: None,
    events_auth: EventsAuth,
    metrics: ReasonerMetrics,
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

        # ---- 3: the remote calls. NO TRANSACTION IS OPEN. ----
        #
        # Long calls to other services must not hold a database connection and a
        # row lock. The transaction above is closed; the one below has not started.
        #
        # 3a: retrieval, when a knowledge service is configured. `fetch` never
        # raises; its three-way outcome is CARRIED, not collapsed:
        #   grounded    -> the bundle's retrieved_context fills, the RCA can cite
        #   empty       -> stays [], honestly ungrounded: no runbook covers this
        #   unavailable -> stays [], ungrounded because retrieval FAILED
        # The last two look identical on the bundle, which is exactly why the
        # metadata rides to the stored wrapper — collapsing them would waste the
        # distinction CRAG and the context API preserved this far.
        knowledge = get_knowledge()
        retrieval: RetrievalMetadata | None = None
        if knowledge is not None:
            fetched = await knowledge.fetch(bundle)
            retrieval = RetrievalMetadata(
                outcome=fetched.outcome.value,
                chunk_count=len(fetched.chunks),
                detail=fetched.detail,
                elapsed_ms=fetched.elapsed_ms,
            )
            # The outcome check is belt-and-braces, not load-bearing:
            # KnowledgeResult enforces structurally that non-grounded results
            # carry no chunks, so copying them unconditionally would be
            # behaviourally identical (a [] over a []). The guard stays as the
            # readable statement of intent; the mutation that deletes it is
            # EQUIVALENT and survives by construction — the enforcement lives in
            # KnowledgeResult.__post_init__ and its test, not here.
            if fetched.outcome is RetrievalOutcome.GROUNDED:
                bundle = bundle.model_copy(update={"retrieved_context": fetched.chunks})

        # 3b: the LLM call. `complete` does not raise (it returns a typed
        # LLMFailure), and neither does the parser inside `resolve`. So there is
        # no exception path between here and the write — and `resolve` is total
        # over the result space. An incident that reaches this line WILL have a
        # recommendation to store.
        llm_result = await gateway.complete(bundle)
        outcome = resolve(bundle, llm_result, retrieval=retrieval)

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
            try:
                stored = await store_recommendation(
                    session,
                    correlation_id=envelope.correlation_id,
                    incident_id=payload.incident_id,
                    plan_id=payload.plan_id,
                    outcome=outcome,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
            except ReferencedRowMissingError as exc:
                # The incident or the plan was deleted while we were waiting on the
                # LLM. Checked explicitly in storage (see its docstring) precisely so
                # it can never arrive here disguised as the duplicate race and be
                # absorbed with a 200. 422 dead-letters it; NO marker, because nothing
                # was written and the event must not look handled.
                #
                # WHICH row vanished is preserved in the detail — a missing incident
                # and a missing plan point at different upstream bugs, and the operator
                # needs to know which one to go looking for.
                log.error(
                    "recommendation.referenced_row_missing",
                    event_id=str(envelope.event_id),
                    incident_id=str(payload.incident_id),
                    plan_id=str(payload.plan_id),
                    missing="incident"
                    if isinstance(exc, IncidentNotFoundError)
                    else "plan",
                    detail=str(exc),
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
            except IntegrityError:
                # THE RACE, and by now it can be NOTHING ELSE. Both foreign keys were
                # checked above, so the only constraint left to violate is the unique
                # index on recommendations.incident_id: two deliveries interleaved
                # between the pre-check and the insert, and this one lost.
                #
                # The other transaction wrote the RCA, so the work IS done — this
                # delivery simply has nothing left to do. A 500 would have the worker
                # retry a race it would only lose again.
                await session.rollback()
                return await _absorb_race(
                    database, envelope=envelope, payload=payload, metrics=metrics
                )

        if stored.duplicate:
            # The SEQUENTIAL duplicate: the pre-check found an existing recommendation.
            # Its audit row committed with the marker above; the log and the counter are
            # here, so both duplicate paths are equally visible.
            metrics.duplicate_recommendation_requests_total.inc()
            log.warning(
                "reasoner.duplicate_recommendation_ignored",
                event_id=str(envelope.event_id),
                incident_id=str(payload.incident_id),
                existing_recommendation_id=str(stored.recommendation_id),
                detected_by="pre_check",
            )
            return {"status": "already_recommended"}

        # ---- COUNTED ONLY NOW. The commit is above; the row is durable. ----
        #
        # Incrementing before the commit would count recommendations that never landed:
        # a transaction that rolls back would still have moved the counter, and the
        # fallback RATE — the number the LLMFallbackRateHigh alert fires on — would be
        # inflated by writes that do not exist. A metric that counts intentions rather
        # than facts is worse than no metric, because it is believed.
        metrics.recommendations_total.labels(
            outcome.llm_provider, outcome.confidence.value
        ).inc()

        fallback = outcome.context_bundle.fallback
        if fallback is not None:
            # Keyed off the metadata, not off `is_fallback` — the two cannot disagree
            # (resolve sets them together), and reading the reason from the same object
            # that proves the fallback happened keeps it that way.
            #
            # The `reason` label is what makes this actionable: `rejected` means fix the
            # config NOW, `gateway_unavailable` means wait for the provider. A bare
            # total cannot tell an operator which.
            metrics.recommendations_fallback_total.labels(fallback.reason.value).inc()

        log.info(
            "recommendation.created",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
            incident_id=str(payload.incident_id),
            recommendation_id=str(stored.recommendation_id),
            is_fallback=outcome.is_fallback,
            confidence=outcome.confidence.value,
            llm_provider=outcome.llm_provider,
        )
        return {"status": "processed"}

    return router


async def _absorb_race(
    database: Database,
    *,
    envelope: EventEnvelope,
    payload: ReasoningRequestedPayload,
    metrics: ReasonerMetrics,
) -> dict[str, str]:
    """Record the lost race in a FRESH transaction, and answer 200.

    The original transaction was rolled back by the ``IntegrityError``, so its marker
    and audit row went with it. They have to be rewritten here — otherwise the worker
    redelivers this event forever, losing the same race every time.

    The marker is re-checked first: the ``IntegrityError`` could also have come from two
    deliveries of the SAME event racing on the ``processed_events`` primary key, in
    which case the marker is already there and writing it again would fail again.
    """
    # Safe to count here: the transaction that could have rolled back ALREADY has —
    # the IntegrityError rolled it back. Nothing below can un-happen this duplicate.
    metrics.duplicate_recommendation_requests_total.inc()
    log.warning(
        "reasoner.duplicate_recommendation_ignored",
        event_id=str(envelope.event_id),
        incident_id=str(payload.incident_id),
        detected_by="unique_index",
    )
    async with database.session() as recovery:
        if await is_already_processed(recovery, envelope.event_id, SERVICE_NAME):
            # The other racer was this same event: it is already recorded.
            return {"status": "already_processed"}
        recovery.add(
            race_audit(
                incident_id=payload.incident_id,
                correlation_id=envelope.correlation_id,
            )
        )
        await mark_processed(recovery, envelope.event_id, SERVICE_NAME)
        await recovery.commit()
    return {"status": "already_recommended"}


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
