"""Store the investigation plan, and ask the reasoner to reason over it.

The planner's write path. Match a template, store the plan, emit
``incident.reasoning_requested``, all in ONE transaction with the caller's
``processed_events`` marker.

**Two ways to get a second plan, needing different defences.**
``investigation_plans`` has a unique index on ``incident_id``: one plan per incident.
The idempotency gate does not protect it, because the gate keys on ``event_id`` and a
second, distinct ``plan_requested`` for the same incident is a different event.

- **Sequentially** (a redelivered-then-re-emitted event, or an upstream bug): the
  pre-check ``SELECT`` finds the existing plan and the planner no-ops.
- **Concurrently** (two deliveries interleaving between the pre-check and the
  insert): both pre-checks see nothing, both insert, and the unique index rejects the
  loser with ``IntegrityError``. That is the real backstop. The tests force the
  interleave with a barrier between pre-check and insert, so removing the ``try``
  around the insert makes them fail with an unhandled ``IntegrityError``.

Either way the answer is 200 with no second plan and no second
``reasoning_requested``: the incident IS planned, and a 500 would have the worker
retry a race it will lose again. Every duplicate still gets a named WARNING log, an
``audit_log`` row, and a counter, so an upstream bug is not absorbed silently.

**The incident is checked first, on purpose.**
``investigation_plans.incident_id`` is a (deferrable) foreign key, so a plan for a
nonexistent incident would also fail at commit with ``IntegrityError``, and the
duplicate handler would swallow a missing incident as if it were a race, answer 200,
and lose the event forever. The check raises :class:`IncidentNotFoundError` (422,
dead-lettered), leaving a genuine race as the only ``IntegrityError`` that remains.
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
"""Every plan is stored ``'pending'`` and, in the POC, stays there.

The column exists (schema Phase 3) but nothing advances it: no code reads or updates
plan status, so this is the only value it ever holds. "Has this plan been reasoned
over?" is already answered authoritatively by whether a ``recommendations`` row exists
for the incident (one per incident, unique-indexed); a status column updated in the
reasoner would be a denormalized copy of that fact. Tracked as carried debt in
docs/roadmap.md."""


class IncidentNotFoundError(RadarError):
    """The incident named by the event payload does not exist.

    The watcher writes its ``plan_requested`` event in the same transaction as the
    incident state it describes, so this means the row was deleted. Left uncaught,
    the plan's foreign key would fail at commit and be indistinguishable from a
    duplicate race, so it is caught explicitly and mapped to 422 (permanent,
    dead-lettered) rather than absorbed.
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
        # The sequential duplicate. The reasoner already has this incident, so a
        # second plan would mean a second LLM call and two RCA cards for one incident.
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
        # The ingress value, passed through, never a fresh UUID: this is the link in
        # the chain the whole pipeline is traced by.
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
    plan; the audit row is the answer.
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
