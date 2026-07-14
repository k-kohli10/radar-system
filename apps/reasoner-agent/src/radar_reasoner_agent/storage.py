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

WHAT THIS MODULE DOES **NOT** DECIDE
------------------------------------
Nothing. Every column is already decided — ``ReasoningOutcome`` carries them, and
``resolve`` set ``is_fallback`` and ``llm_provider`` together so they cannot disagree.
This module is a MAPPING, deliberately: a second place that reasoned about which
provider to record would be a second place that could get it wrong, and the two would
drift in exactly the way the fallback contract exists to prevent.

So there is no logic here to test — which is why the tests for it are about the
DATABASE: that the row lands, that it survives the commit, and that the columns say the
same thing the object did.

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

from typing import Any
from uuid import UUID

from radar_common import get_logger, new_id
from radar_contracts import RecommendationCreatedPayload
from radar_database import AuditLog, Recommendation, write_outbox_event
from sqlalchemy.ext.asyncio import AsyncSession

from radar_reasoner_agent.config import SERVICE_NAME
from radar_reasoner_agent.fallback import ReasoningOutcome

log = get_logger("reasoner.storage")

RECOMMENDATION_CREATED_EVENT = "recommendation.created"
"""Outbox event type the reasoner emits once an incident has its RCA."""

FEEDBACK_TARGET = "feedback-service"
"""Target service for ``recommendation.created``. Does not exist until Phase 9."""

AUDIT_RECOMMENDATION_CREATED = "reasoner.recommendation_created"


async def store_recommendation(
    session: AsyncSession,
    *,
    correlation_id: UUID,
    incident_id: UUID,
    plan_id: UUID,
    outcome: ReasoningOutcome,
) -> UUID:
    """Write the recommendation, the outbox event and the audit row. Does NOT commit.

    The caller owns the transaction boundary, so all three land with the caller's
    ``processed_events`` marker as one atomic unit. Returns the new recommendation's id.
    """
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
    return recommendation.id


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
