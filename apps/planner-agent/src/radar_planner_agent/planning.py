"""Store the investigation plan, and ask the reasoner to reason over it.

The planner's write path. Match a template, store the plan, emit
``incident.reasoning_requested`` — all in ONE transaction with the caller's
``processed_events`` marker, so a crash between them is impossible.

TWO WAYS TO GET A SECOND PLAN, AND THEY NEED DIFFERENT DEFENCES
---------------------------------------------------------------
``investigation_plans`` has a unique index on ``incident_id``: one plan per
incident. The idempotency gate does NOT protect it, because the gate keys on
``event_id`` and a *second, distinct* ``plan_requested`` for the same incident is a
different event entirely.

- **Sequentially** (a redelivered-then-re-emitted event, or an upstream bug): the
  pre-check ``SELECT`` finds the existing plan and the planner no-ops.
- **Concurrently** (two deliveries interleaving between the pre-check and the
  insert): both pre-checks see nothing, both insert, and the unique index rejects
  the loser with ``IntegrityError``. That is the real backstop, and it is the only
  thing standing between a race and two investigations for one incident.

The concurrent path is easy to leave decorative — an assertion that a code branch
exists, never actually exercised. It is exercised: the tests force the interleave
with a barrier between the pre-check and the insert, and removing the ``try``
around the insert makes them fail with an unhandled ``IntegrityError``.

Either way the answer is 200 with no second plan and no second
``reasoning_requested`` — the incident IS planned, so re-planning is unnecessary
rather than erroneous, and a 500 would have the worker retry a race it will simply
lose again. But absorbing an upstream bug silently is how it stays a bug, so every
duplicate is a named WARNING log, an ``audit_log`` row, and a counter.

THE INCIDENT IS CHECKED FIRST, ON PURPOSE
-----------------------------------------
``investigation_plans.incident_id`` is a (deferrable) foreign key, so a plan for a
nonexistent incident would ALSO fail at commit with ``IntegrityError`` — and the
duplicate handler would then swallow a missing incident as if it were a race,
answer 200, and lose the event forever. So the incident's existence is checked
explicitly and raises :class:`IncidentNotFoundError` (→ 422, dead-lettered, visible
to a human). After that check, the only ``IntegrityError`` left is a genuine race.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from radar_common import RadarError, get_logger, new_id
from radar_contracts import PlanRequestedPayload, ReasoningRequestedPayload
from radar_database import AuditLog, Incident, InvestigationPlan, write_outbox_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from radar_planner_agent.templates import PlanTemplates

log = get_logger("planner.planning")

REASONING_REQUESTED_EVENT = "incident.reasoning_requested"
"""Outbox event type the planner emits to start the reasoning stage."""

REASONER_TARGET = "reasoner-agent"
"""Target service for the ``incident.reasoning_requested`` event."""

AUDIT_PLAN_CREATED = "planner.plan_created"
AUDIT_DUPLICATE_IGNORED = "planner.duplicate_plan_ignored"

PLAN_STATUS_PENDING = "pending"
"""A plan is 'pending' until the reasoner has reasoned over it."""


class IncidentNotFoundError(RadarError):
    """The incident named by the event payload does not exist.

    Should be unreachable: the watcher writes its ``plan_requested`` event in the
    same transaction as the incident state it describes, so an event that exists
    implies an incident that exists. If it happens, the plan's foreign key would
    fail at commit and be indistinguishable from a duplicate race — so it is caught
    explicitly and mapped to 422 (permanent → dead-letter), surfacing to a human
    rather than being silently absorbed.
    """


@dataclass(frozen=True, slots=True)
class PlanningOutcome:
    """What the planner did with one ``plan_requested`` event."""

    incident_id: UUID
    plan_id: UUID
    template_key: str
    is_default: bool
    duplicate: bool


async def plan_incident(
    session: AsyncSession,
    *,
    templates: PlanTemplates,
    correlation_id: UUID,
    payload: PlanRequestedPayload,
) -> PlanningOutcome:
    """Store the investigation plan and request reasoning. Does NOT commit.

    The caller owns the transaction boundary, so the plan, the outbox event, the
    audit row, and the caller's ``processed_events`` marker are one atomic unit.

    Raises :class:`IncidentNotFoundError` if the incident is gone. Returns a
    ``duplicate`` outcome if this incident already has a plan (the sequential case);
    the concurrent case surfaces as ``IntegrityError`` at commit and is handled by
    the caller.
    """
    incident = await session.get(Incident, payload.incident_id)
    if incident is None:
        raise IncidentNotFoundError(
            f"incident {payload.incident_id} does not exist; the watcher writes the "
            "event and the incident state together, so this row was deleted"
        )

    existing = await _existing_plan_id(session, payload.incident_id)
    if existing is not None:
        # The sequential duplicate. The incident is already planned and the reasoner
        # already has it: a second plan would mean a second investigation, a second
        # LLM call, and two RCA cards for one incident.
        session.add(
            _audit(
                AUDIT_DUPLICATE_IGNORED,
                incident_id=payload.incident_id,
                correlation_id=correlation_id,
                payload=payload,
                extra={"existing_plan_id": str(existing)},
            )
        )
        return PlanningOutcome(
            incident_id=payload.incident_id,
            plan_id=existing,
            template_key="",
            is_default=False,
            duplicate=True,
        )

    match = templates.match(payload.service_name, payload.alert_name)
    plan = InvestigationPlan(
        id=new_id(),
        incident_id=payload.incident_id,
        correlation_id=correlation_id,
        # The stored shape IS the PlanStep contract shape (order, description), so
        # the reasoner and the Slack card read it without a translation layer.
        steps=[step.model_dump() for step in match.template.ordered_steps],
        template_key=match.key,
        status=PLAN_STATUS_PENDING,
    )
    session.add(plan)

    body = ReasoningRequestedPayload(incident_id=payload.incident_id, plan_id=plan.id)
    await write_outbox_event(
        session,
        event_type=REASONING_REQUESTED_EVENT,
        target_service=REASONER_TARGET,
        payload=body.model_dump(mode="json"),
        # The ingress value, passed through — never a fresh UUID. This is the link
        # in the chain the whole pipeline is traced by.
        correlation_id=correlation_id,
    )
    session.add(
        _audit(
            AUDIT_PLAN_CREATED,
            incident_id=payload.incident_id,
            correlation_id=correlation_id,
            payload=payload,
            extra={
                "plan_id": str(plan.id),
                "template_key": match.key,
                "is_default": match.is_default,
                "step_count": len(match.template.steps),
            },
        )
    )
    return PlanningOutcome(
        incident_id=payload.incident_id,
        plan_id=plan.id,
        template_key=match.key,
        is_default=match.is_default,
        duplicate=False,
    )


async def _existing_plan_id(session: AsyncSession, incident_id: UUID) -> UUID | None:
    """The id of this incident's plan, if it already has one.

    A module-level function, not an inline query, so the tests can wrap it to force
    the concurrent interleave the unique index exists to catch.
    """
    result = await session.scalar(
        select(InvestigationPlan.id).where(InvestigationPlan.incident_id == incident_id)
    )
    return result


def _audit(
    event_type: str,
    *,
    incident_id: UUID,
    correlation_id: UUID,
    payload: PlanRequestedPayload,
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """The append-only record of what the planner decided.

    Written for the duplicate case too. A silent no-op is indistinguishable from a
    bug when someone asks why an incident got two plan_requested events and only one
    plan — the audit row is the answer.
    """
    body: dict[str, Any] = {
        "service_name": payload.service_name,
        "alert_name": payload.alert_name,
    }
    if extra:
        body.update(extra)
    return AuditLog(
        event_type=event_type,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=correlation_id,
        actor="planner-agent",
        payload=body,
    )


def duplicate_audit(
    *,
    incident_id: UUID,
    correlation_id: UUID,
    payload: PlanRequestedPayload,
    reason: str,
) -> AuditLog:
    """The audit row for a duplicate caught by the unique index (the race).

    The sequential duplicate is audited inside :func:`plan_incident`; this one
    cannot be, because that transaction is rolled back. The caller writes it in the
    recovery transaction instead.
    """
    return _audit(
        AUDIT_DUPLICATE_IGNORED,
        incident_id=incident_id,
        correlation_id=correlation_id,
        payload=payload,
        extra={"reason": reason},
    )
