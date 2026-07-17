"""The ``POST /events`` endpoint: idempotency first, then the plan.

The outbox worker delivers at *least* once — a dispatch that times out, or a
worker that dies after delivering but before recording the result, is
redelivered. So the first thing this handler does, before any interpretation of
the event at all, is ask ``processed_events`` whether this ``event_id`` has
already been handled by ``planner-agent``. If it has, the answer is 200 and
nothing else happens.

Everything the event changes is in the SAME transaction as the marker:

    processed_events  +  investigation_plan  +  reasoning_requested outbox event
                      +  audit_log

Committed together, so a crash between them is impossible. A marker committed
without its plan would be quietly terminal: the incident would never be planned,
and the gate would then ensure it was never planned on redelivery *either*,
because the marker says the work was done. The pipeline would stop for that
incident, permanently and silently.

THE DUPLICATE PLAN, AND WHY IT IS NOT A 500
-------------------------------------------
A *second, distinct* ``plan_requested`` for an incident that already has a plan
gets past the idempotency gate (different ``event_id``) and hits the unique index
on ``investigation_plans.incident_id``. Sequentially the pre-check catches it;
concurrently the index does, with an ``IntegrityError``.

Both answer 200 with no second plan and no second ``reasoning_requested``. The
incident IS planned and the reasoner already has it, so re-planning is unnecessary
rather than erroneous — and a 500 would only have the worker retry a race it will
lose again, eventually dead-lettering an event whose work was already done.

But a 200 absorbs an upstream bug, and silence is how a bug survives. So every
duplicate is a named WARNING log, an ``audit_log`` row, and the
``radar_duplicate_plan_requests_total`` counter — a non-zero rate means the watcher
is double-emitting, and it says so on a dashboard.

Status codes are the documented agent contract: 200 processed (or already seen),
401 bad token, 422 malformed payload — and 401 beats 422 (the shared guard in
``radar_common.auth``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from radar_common import EventsAuth, bind_correlation_id, get_logger
from radar_contracts import EventEnvelope, PlanRequestedPayload
from radar_database import Database, is_already_processed, mark_processed
from radar_telemetry import PlannerMetrics
from sqlalchemy.exc import IntegrityError

from radar_planner_agent.config import SERVICE_NAME
from radar_planner_agent.planning import (
    IncidentNotFoundError,
    duplicate_audit,
    plan_incident,
)
from radar_planner_agent.templates import PlanTemplates

log = get_logger("planner.routes")

PLAN_REQUESTED_EVENT = "incident.plan_requested"
"""The only event type the planner consumes."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    get_templates: Callable[[], PlanTemplates | None],
    events_auth: EventsAuth,
    metrics: PlannerMetrics,
) -> APIRouter:
    """Build the ``POST /events`` surface.

    ``get_database`` and ``get_templates`` return the live objects (set during
    startup) or ``None`` when the service is not ready — the handler answers 503
    then, rather than planning against templates nobody configured.
    """
    router = APIRouter()

    @router.post("/events", dependencies=[Depends(events_auth.require())])
    async def receive_event(envelope: EventEnvelope) -> dict[str, str]:
        # Bind before anything else can log: every line this request produces,
        # including a rejection, carries the correlation id minted at ingress, so
        # the pipeline stays traceable by that value alone.
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        templates = get_templates()
        if database is None or templates is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="planner-agent is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all: a redelivery must not be able to reach any work.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != PLAN_REQUESTED_EVENT:
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
                outcome = await plan_incident(
                    session,
                    templates=templates,
                    correlation_id=envelope.correlation_id,
                    payload=payload,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
            except IncidentNotFoundError as exc:
                # Checked explicitly (see planning) so it can never be mistaken for
                # a duplicate race and silently absorbed. 422 dead-letters it, and a
                # human sees it. No marker: nothing was decided.
                log.error("incident.not_found", event_id=str(envelope.event_id))
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
            except IntegrityError:
                # THE RACE. Two deliveries interleaved between the pre-check and the
                # insert; the unique index on investigation_plans.incident_id
                # rejected this one. The other transaction planned the incident, so
                # the work IS done — this delivery just has nothing left to do.
                await session.rollback()
                return await _absorb_duplicate(
                    database, envelope=envelope, payload=payload, metrics=metrics
                )

        if outcome.duplicate:
            # The SEQUENTIAL duplicate: the pre-check found an existing plan. Its
            # audit row committed with the marker above; the log and counter are
            # here, so both duplicate paths are equally visible.
            metrics.duplicate_plan_requests_total.inc()
            log.warning(
                "planner.duplicate_plan_ignored",
                event_id=str(envelope.event_id),
                incident_id=str(outcome.incident_id),
                existing_plan_id=str(outcome.plan_id),
                detected_by="pre_check",
            )
            return {"status": "already_planned"}

        # matched vs default: the live signal that a template key has drifted. A
        # climbing default rate is the only symptom of the silent-fallback bug.
        metrics.plans_created_total.labels(
            "default" if outcome.is_default else "matched"
        ).inc()
        if outcome.is_default:
            log.warning(
                "planner.template_defaulted",
                incident_id=str(outcome.incident_id),
                # The key that MISSED — so the fix is a grep away, not a guess.
                missed_key=f"{payload.service_name}:{payload.alert_name}",
            )
        log.info(
            "plan.created",
            event_id=str(envelope.event_id),
            incident_id=str(outcome.incident_id),
            plan_id=str(outcome.plan_id),
            template_key=outcome.template_key,
            is_default=outcome.is_default,
        )
        return {"status": "processed"}

    return router


async def _absorb_duplicate(
    database: Database,
    *,
    envelope: EventEnvelope,
    payload: PlanRequestedPayload,
    metrics: PlannerMetrics,
) -> dict[str, str]:
    """Record the lost race in a FRESH transaction, and answer 200.

    The original transaction is rolled back, so its marker and audit row are gone
    with it — they have to be rewritten here or the worker would redeliver this
    event forever, losing the same race every time.

    The marker is re-checked first: the ``IntegrityError`` could also have come from
    two deliveries of the SAME event racing on the ``processed_events`` primary key,
    in which case the marker is already there and writing it again would fail again.
    """
    metrics.duplicate_plan_requests_total.inc()
    log.warning(
        "planner.duplicate_plan_ignored",
        event_id=str(envelope.event_id),
        incident_id=str(payload.incident_id),
        detected_by="unique_index",
    )
    async with database.session() as recovery:
        if await is_already_processed(recovery, envelope.event_id, SERVICE_NAME):
            # The other racer was this same event: it is already recorded.
            return {"status": "already_processed"}
        recovery.add(
            duplicate_audit(
                incident_id=payload.incident_id,
                correlation_id=envelope.correlation_id,
                payload=payload,
                reason="lost the race to the one-plan-per-incident index",
            )
        )
        await mark_processed(recovery, envelope.event_id, SERVICE_NAME)
        await recovery.commit()
    return {"status": "already_planned"}


def _parse_payload(envelope: EventEnvelope) -> PlanRequestedPayload:
    """Validate the event body against the shape the watcher promised to send.

    The envelope's ``payload`` is an open dict by design — the envelope is generic
    transport — so the per-event-type shape is judged here, by the agent that knows
    what its own events mean. The watcher *constructs* this same model, so a
    mismatch is a real malformation, and 422 is the honest answer (the worker treats
    it as permanent and dead-letters it, rather than retrying a body that will never
    parse).
    """
    try:
        return PlanRequestedPayload.model_validate(envelope.payload)
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
