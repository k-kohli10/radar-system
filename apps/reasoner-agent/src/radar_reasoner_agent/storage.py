"""Store the recommendation, and tell the feedback service it exists.

The reasoner's write path, and the last stage of the pipeline. One transaction with the
caller's ``processed_events`` marker, so a crash between them is impossible:

    recommendation  +  recommendation.created outbox  +  audit_log  +  marker

The marker is the reason the boundary matters. It is what makes a redelivery a no-op —
so it must land with the recommendation, never before it. If the marker committed first
and the insert failed, the event would be marked handled with nothing written, and the
incident would be permanently un-recommended: the invariant defeated not by a missing
fallback, but by a transaction boundary in the wrong place. R6 built the outcome so this
could not happen; this module keeps that promise to the database.

THREE WAYS TO GET AN ``IntegrityError`` AT COMMIT, AND ONLY ONE IS BENIGN
------------------------------------------------------------------------
The planner had two (its unique index, and its one foreign key). ``recommendations`` has
**two foreign keys** — ``incident_id`` AND ``plan_id`` — so there are three:

    1. the unique index (idx_recommendations_one_per_incident)  -> a genuine RACE
    2. incident_id references an incident that is gone          -> DATA LOSS
    3. plan_id references a plan that is gone                   -> DATA LOSS  (new)

They arrive as **the same exception, at the same moment**, and the handler answers the
race with a 200 no-op. So an unguarded insert would answer 200 to cases 2 and 3 as well:
mark the event processed, write nothing, and lose the incident's RCA forever while
reporting success. That is the silent-data-loss bug, and the second FK is a second door
into it that the planner never had.

Both FKs are therefore checked EXPLICITLY, BEFORE the insert. The ordering is the whole
mechanism: rule out 2 and 3 by hand, and the only ``IntegrityError`` that can still
reach the caller is 1 — a race, which is safe to absorb. The two checks raise
DIFFERENT exceptions on purpose, and the handler maps them to different 422s: "the
incident vanished" and "the plan vanished" point at different upstream bugs, and an
operator woken at 3am needs to know which row to go looking for. Collapsing them into
"some FK is missing" would throw that away, for nothing.

WHY THE CONTEXT BUILDER'S CHECK IS NOT ENOUGH
---------------------------------------------
``build_context_bundle`` already refuses a missing incident or plan — in transaction
ONE. This is transaction TWO, and between them sits the LLM call: **tens of seconds**
with no transaction open, which is precisely the window in which a cleanup job, an
operator, or a stale replay can delete the rows out from under an in-flight
recommendation. Re-checking here is not belt-and-braces; it covers a window the first
check cannot see, and it is the only thing standing between that window and case 2 or 3.

Is a vanished plan REACHABLE in normal operation? No — the planner writes the plan and
the ``reasoning_requested`` event in one transaction, so an event that exists implies a
plan that exists. This is defence against the should-be-impossible, in the same
category as R3's mismatched incident/plan pair. It stays because the trade is one-sided:
the check costs a single indexed SELECT, and getting it wrong costs an incident its RCA,
silently, with the event marked handled. A shouldn't-happen that happened is exactly
what a 422 and a dead-letter queue are for.

WHAT THIS MODULE DOES **NOT** DECIDE
------------------------------------
Which columns to write. Every one is already decided — ``ReasoningOutcome`` carries
them, and ``resolve`` set ``is_fallback`` and ``llm_provider`` together so they cannot
disagree. The INSERT is a MAPPING, deliberately: a second place that reasoned about
which provider to record would be a second place that could get it wrong.

THE EVENT NAMES THE RECOMMENDATION, IT DOES NOT COPY IT
-------------------------------------------------------
``recommendation.created`` carries two ids. The feedback service reads the row. See
``RecommendationCreatedPayload`` — a recommendation is the one row a human can later
*correct*, and a payload carrying a frozen copy of the root cause could contradict the
corrected row it names.

It will DEAD-LETTER until Phase 9 builds ``feedback-service``, and that is correct
rather than a gap: the event is the durable record that the RCA is ready for delivery,
the outbox retries it, and the dead-letter queue is exactly where an undeliverable event
belongs. The POC's end-to-end test reads the recommendation row straight from Postgres,
so nothing depends on the delivery landing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from radar_common import RadarError, get_logger, new_id
from radar_contracts import RecommendationCreatedPayload
from radar_database import (
    AuditLog,
    Incident,
    InvestigationPlan,
    Recommendation,
    write_outbox_event,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from radar_reasoner_agent.config import SERVICE_NAME
from radar_reasoner_agent.fallback import ReasoningOutcome

log = get_logger("reasoner.storage")

RECOMMENDATION_CREATED_EVENT = "recommendation.created"
"""Outbox event type the reasoner emits once an incident has its RCA."""

FEEDBACK_TARGET = "feedback-service"
"""Target service for ``recommendation.created``. Does not exist until Phase 9."""

AUDIT_RECOMMENDATION_CREATED = "reasoner.recommendation_created"
AUDIT_DUPLICATE_IGNORED = "reasoner.duplicate_recommendation_ignored"


class ReferencedRowMissingError(RadarError):
    """A row the recommendation must point at is gone. Never a race — see the module.

    Deliberately a base class with two distinct subclasses rather than one error with a
    ``which`` field: the caller maps them to different 422s, and a missing incident and
    a missing plan point at different upstream bugs.
    """


class IncidentNotFoundError(ReferencedRowMissingError):
    """The incident vanished between the context read and the write."""


class PlanNotFoundError(ReferencedRowMissingError):
    """The investigation plan vanished between the context read and the write.

    The door the planner never had. ``recommendations`` has a second foreign key, and
    without this check a deleted plan would raise ``IntegrityError`` at commit — the
    same exception the race raises — and be absorbed as a duplicate: 200, marker
    written, RCA lost.
    """


@dataclass(frozen=True, slots=True)
class StorageOutcome:
    """What the write path did with one recommendation."""

    recommendation_id: UUID
    #: True when this incident already had a recommendation (the SEQUENTIAL duplicate,
    #: caught by the pre-check). The CONCURRENT one surfaces as ``IntegrityError`` at
    #: commit and is the caller's to absorb.
    duplicate: bool


async def store_recommendation(
    session: AsyncSession,
    *,
    correlation_id: UUID,
    incident_id: UUID,
    plan_id: UUID,
    outcome: ReasoningOutcome,
) -> StorageOutcome:
    """Write the recommendation, the outbox event and the audit row. Does NOT commit.

    The caller owns the transaction boundary, so all three land with the caller's
    ``processed_events`` marker as one atomic unit.

    Raises :class:`IncidentNotFoundError` or :class:`PlanNotFoundError` if either
    referenced row is gone — checked HERE, before the insert, so that the only
    ``IntegrityError`` the caller can still see is a genuine race. See the module
    docstring: that ordering is the entire mechanism.
    """
    # ---- FK source 2. Ruled out by hand, because the FK cannot tell the caller. ----
    if await session.get(Incident, incident_id) is None:
        raise IncidentNotFoundError(
            f"incident {incident_id} does not exist; it was deleted while the reasoner "
            "was waiting on the LLM. Refusing to write an RCA for an incident that is "
            "not there — the foreign key would fail at COMMIT, indistinguishable from "
            "a duplicate race, and the event would be silently lost."
        )

    # ---- FK source 3. The one the planner never had. ----
    if await session.get(InvestigationPlan, plan_id) is None:
        raise PlanNotFoundError(
            f"investigation plan {plan_id} does not exist; it was deleted while the "
            "reasoner was waiting on the LLM. Same trap as the incident above, through "
            "the second foreign key."
        )

    # ---- The SEQUENTIAL duplicate. The concurrent one is the unique index's job. ----
    existing = await _existing_recommendation_id(session, incident_id)
    if existing is not None:
        session.add(
            _duplicate_audit(
                incident_id=incident_id,
                correlation_id=correlation_id,
                existing_id=existing,
                reason="the pre-check found an existing recommendation",
            )
        )
        return StorageOutcome(recommendation_id=existing, duplicate=True)

    recommendation = Recommendation(
        id=new_id(),
        incident_id=incident_id,
        plan_id=plan_id,
        # The ingress value, passed through — never a fresh UUID. This is the last link
        # in the chain Phase 10 traces an incident by.
        correlation_id=correlation_id,
        root_cause=outcome.root_cause,
        confidence=outcome.confidence.value,
        recommended_actions=[a.model_dump() for a in outcome.recommended_actions],
        # mode="json": the bundle holds a UUID and a datetime, and JSONB takes neither.
        context_bundle=outcome.context_bundle.model_dump(mode="json"),
        # ---- Copied, not re-derived. `resolve` set these together. ----
        is_fallback=outcome.is_fallback,
        llm_provider=outcome.llm_provider,
        model_alias=outcome.model_alias,
        model_id=outcome.model_id,
        # ---- The four columns that describe the call. All four, or none. ----
        raw_llm_response=outcome.raw_llm_response,
        prompt_tokens=outcome.prompt_tokens,
        completion_tokens=outcome.completion_tokens,
        latency_ms=outcome.latency_ms,
    )
    session.add(recommendation)

    body = RecommendationCreatedPayload(
        incident_id=incident_id, recommendation_id=recommendation.id
    )
    await write_outbox_event(
        session,
        event_type=RECOMMENDATION_CREATED_EVENT,
        target_service=FEEDBACK_TARGET,
        payload=body.model_dump(mode="json"),
        correlation_id=correlation_id,
    )

    session.add(
        _audit(
            incident_id=incident_id,
            correlation_id=correlation_id,
            recommendation_id=recommendation.id,
            outcome=outcome,
        )
    )
    return StorageOutcome(recommendation_id=recommendation.id, duplicate=False)


async def _existing_recommendation_id(
    session: AsyncSession, incident_id: UUID
) -> UUID | None:
    """This incident's recommendation, if it already has one.

    A module-level function, not an inline query, so the tests can wrap it to force the
    concurrent interleave the unique index exists to catch. A backstop that is never
    actually raced is a branch nobody has proven works.
    """
    existing: UUID | None = await session.scalar(
        select(Recommendation.id).where(Recommendation.incident_id == incident_id)
    )
    return existing


def _duplicate_audit(
    *,
    incident_id: UUID,
    correlation_id: UUID,
    existing_id: UUID,
    reason: str,
) -> AuditLog:
    """The record of a duplicate that was ignored — from either guard.

    Absorbing a duplicate silently is how an upstream bug stays a bug. Two
    ``reasoning_requested`` events for one incident is not normal, and the audit row is
    what answers "why does this incident have two events and one recommendation?".
    """
    return AuditLog(
        event_type=AUDIT_DUPLICATE_IGNORED,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=correlation_id,
        actor=SERVICE_NAME,
        payload={"existing_recommendation_id": str(existing_id), "reason": reason},
    )


def race_audit(*, incident_id: UUID, correlation_id: UUID) -> AuditLog:
    """The audit row for a duplicate caught by the unique index (the concurrent race).

    The sequential duplicate is audited inside :func:`store_recommendation`; this one
    cannot be, because that transaction is rolled back by the ``IntegrityError``. The
    caller writes it in the recovery transaction instead.

    The winner's recommendation id is not known here — reading it would mean another
    query in the recovery path to name a row the operator can find from ``incident_id``
    anyway.
    """
    return AuditLog(
        event_type=AUDIT_DUPLICATE_IGNORED,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=correlation_id,
        actor=SERVICE_NAME,
        payload={
            "reason": "lost the race to the one-recommendation-per-incident index"
        },
    )


def _audit(
    *,
    incident_id: UUID,
    correlation_id: UUID,
    recommendation_id: UUID,
    outcome: ReasoningOutcome,
) -> AuditLog:
    """The append-only record of what the reasoner concluded, and at what cost.

    Carries ``is_fallback`` and the fallback reason, so "why did this incident get a
    checklist instead of an analysis?" is answerable from the audit trail alone —
    without joining to a context bundle or reading a log that has since rotated away.
    """
    fallback = outcome.context_bundle.fallback
    body: dict[str, Any] = {
        "recommendation_id": str(recommendation_id),
        "is_fallback": outcome.is_fallback,
        "confidence": outcome.confidence.value,
        "llm_provider": outcome.llm_provider,
        "model_id": outcome.model_id,
        "action_count": len(outcome.recommended_actions),
        # None on a real analysis; the reason on every fallback.
        "fallback_reason": fallback.reason.value if fallback else None,
        # Real spend on a wasted call, None when no call completed. Both are facts.
        "prompt_tokens": outcome.prompt_tokens,
    }
    return AuditLog(
        event_type=AUDIT_RECOMMENDATION_CREATED,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=correlation_id,
        actor=SERVICE_NAME,
        payload=body,
    )
