"""The context bundle: what the model is told about the incident.

Two rows into a dict. The only interesting thing about this module is *which row each
field comes from*, and getting that wrong produces an RCA card that is confidently
out of date.

THE SOURCE-OF-TRUTH SPLIT
-------------------------
**Mutable incident state — severity, status, alert_count — comes from the INCIDENTS
row.** Never from an event payload, and never from a snapshot taken earlier in the
pipeline. An incident can escalate between being planned and being reasoned about: the
planner may have looked at a ``high`` incident that is now ``critical``, and the card
an engineer reads must say what the incident **is**, not what it was when somebody
last glanced at it. This is the payoff for stripping ``severity`` and ``alert_count``
out of ``PlanRequestedPayload`` and ``ReasoningRequestedPayload`` — the stale read is
now not merely discouraged, it is unrepresentable: there is no stale copy to read.

**Investigation steps come from the PLAN row, and that is correct.** Steps are the
plan's *output*. They do not drift after planning — the plan is a decision that was
made, not a snapshot of something that keeps moving. Reading them from the plan is not
a source-of-truth violation, and re-deriving them by re-matching the template would be
worse: it would silently change an in-flight investigation if someone edited the
ConfigMap.

The distinction is worth stating because the rule is easy to over-apply. *Was decided*
comes from the row that decided it. *Is currently true* comes from the row that owns
it.

``alert_name`` comes from the ALERTS rows, because ``incidents`` has no such column.
Every alert on an incident shares its fingerprint, and the fingerprint embeds the alert
name, so any of them answers — the earliest is taken, for determinism.

``retrieved_context`` is the Phase 8 RAG slot: always ``[]`` for now. It is in the v1
shape on purpose, so the prompt and the stored bundle do not change shape when
retrieval lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from radar_common import RadarError, get_logger
from radar_contracts import PlanStep, Severity
from radar_database import Alert, Incident, InvestigationPlan
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger("reasoner.context")


class ContextNotAvailableError(RadarError):
    """The incident or plan this event names does not exist, or they disagree.

    Should be unreachable: the planner writes the plan and the
    ``incident.reasoning_requested`` event in one transaction, and the plan's incident
    is a foreign key. If it happens, the row was deleted or the event was forged —
    either way the reasoner has nothing to reason about, and the honest answer is to
    fail loudly (422 → dead-letter → a human) rather than invent an RCA about an
    incident that is not there.
    """


class ContextBundle(BaseModel):
    """Everything the model is told about one incident. The v1 shape from the plan.

    Stored verbatim on the recommendation (``recommendations.context_bundle``), so a
    future reader can see exactly what the model was shown when it said what it said.
    That is the whole point of keeping it: an RCA you cannot reconstruct the inputs of
    is an RCA you cannot audit.
    """

    model_config = ConfigDict(extra="forbid")

    incident_id: UUID
    service_name: str
    alert_name: str
    #: LIVE, from the incidents row — not the severity of the alert that fired, and
    #: not the severity the planner saw. See the module docstring.
    severity: Severity
    opened_at: datetime
    #: LIVE, from the incidents row.
    alert_count: int
    #: From the PLAN row: a decision that was made, not a value that drifts.
    investigation_steps: list[PlanStep]
    #: Phase 8's retrieval slot. Empty in v1, and in the shape now so the prompt does
    #: not change when it fills.
    retrieved_context: list[dict[str, Any]] = Field(default_factory=list)


async def build_context_bundle(
    session: AsyncSession, *, incident_id: UUID, plan_id: UUID
) -> ContextBundle:
    """Assemble the bundle from the incident row, the plan row, and the alerts.

    Raises :class:`ContextNotAvailableError` if either row is missing, or if the plan
    belongs to a different incident than the event claims.
    """
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise ContextNotAvailableError(
            f"incident {incident_id} does not exist; the planner writes the plan and "
            "the event in one transaction, so this row was deleted"
        )

    plan = await session.get(InvestigationPlan, plan_id)
    if plan is None:
        raise ContextNotAvailableError(
            f"investigation plan {plan_id} does not exist; there is nothing to reason "
            "over"
        )

    if plan.incident_id != incident.id:
        # The event pairs a plan with an incident it does not belong to. Reasoning
        # anyway would produce an RCA about one incident using another's checklist.
        raise ContextNotAvailableError(
            f"plan {plan_id} belongs to incident {plan.incident_id}, not "
            f"{incident_id}; refusing to reason over a mismatched pair"
        )

    alert_name = await _alert_name_for(session, incident.id)

    return ContextBundle(
        incident_id=incident.id,
        service_name=incident.service_name,
        alert_name=alert_name,
        # ---- LIVE incident state. The incidents row owns these. ----
        severity=Severity(incident.severity),
        alert_count=incident.alert_count,
        opened_at=incident.opened_at,
        # ---- The plan's output. Decided, not drifting. ----
        investigation_steps=[PlanStep.model_validate(s) for s in plan.steps],
        # ---- Phase 8. ----
        retrieved_context=[],
    )


async def _alert_name_for(session: AsyncSession, incident_id: UUID) -> str:
    """The alert name for this incident, from its alerts.

    ``incidents`` has no ``alert_name`` column — the watcher is the last stage that
    holds it, and it passes it to the planner on the event. By the reasoner's turn the
    only durable copy is on the ``alerts`` rows.

    Every alert on an incident deduplicated onto it by fingerprint, and the fingerprint
    embeds the alert name, so they all agree; the earliest is taken for determinism.
    Note this reads ``alert_name`` and NOT ``severity`` — the alert's severity is what
    that alert fired at, which is exactly the stale value escalation makes wrong.
    """
    name = await session.scalar(
        select(Alert.alert_name)
        .where(Alert.incident_id == incident_id)
        .order_by(Alert.received_at.asc())
        .limit(1)
    )
    if name is None:
        raise ContextNotAvailableError(
            f"incident {incident_id} has no alerts; ingestion writes the incident and "
            "its first alert in one transaction, so this row was deleted"
        )
    return str(name)
