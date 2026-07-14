"""The invariant at the ROW level: every trigger becomes a recommendation that PERSISTS.

R6 proved the invariant against the type system — every trigger *resolves* to a fallback
outcome, and ``assert_never`` makes a missed case a compile error. That proof stops at
the object. This one starts where it stopped.

The gap between them is a transaction boundary, and it is a real place to lose a write:
the reasoner reads in one transaction, calls the LLM with **no transaction open**, then
writes in a second. An outcome that exists in memory is not an RCA anybody will ever
read. So every assertion below re-reads the row from a **fresh session after the
commit** — never from the session that wrote it, which would hand back an object out of
its identity map whether or not Postgres ever accepted it.

    R6  "we always PRODUCE a recommendation"   (object, compile-time)
    R7b "we always STORE the one we produced"  (row, after commit)

Both are needed. Neither implies the other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from _pytest.mark import ParameterSet
from radar_common import new_id
from radar_contracts import Confidence, LLMMode, PlanStep, Severity
from radar_database import (
    Alert,
    AuditLog,
    Database,
    Incident,
    InvestigationPlan,
    OutboxEvent,
    ProcessedEvent,
    Recommendation,
    mark_processed,
)
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.fallback import FALLBACK_PROVIDER, ReasoningOutcome, resolve
from radar_reasoner_agent.llm import LLMFailure, LLMFailureReason, LLMResult, LLMSuccess
from radar_reasoner_agent.rca import RCAParseFailureReason
from radar_reasoner_agent.storage import (
    FEEDBACK_TARGET,
    RECOMMENDATION_CREATED_EVENT,
    store_recommendation,
)
from sqlalchemy import func, select

GOOD_RCA = (
    '{"root_cause": "A bad deploy broke order validation.", "confidence": "high", '
    '"recommended_actions": [{"order": 1, "action": "kubectl rollout undo"}]}'
)

UNPARSEABLE: dict[RCAParseFailureReason, str] = {
    RCAParseFailureReason.NOT_JSON: "I'm sorry, I can't determine the root cause.",
    RCAParseFailureReason.SCHEMA_INVALID: (
        '{"root_cause": "Something broke.", "confidence": "very high", '
        '"recommended_actions": []}'
    ),
}


def _success(content: str) -> LLMSuccess:
    return LLMSuccess(
        content=content,
        provider="openai",
        model="gpt-4o",
        mode=LLMMode.EXTENDED.value,
        prompt_tokens=420,
        completion_tokens=99,
        latency_ms=8_500,
    )


def _every_failing_result() -> list[ParameterSet]:
    """Every trigger, driven off the enums. Not hand-written; see test_fallback."""
    cases = [
        pytest.param(
            LLMFailure(reason=r, detail=f"simulated {r.value}", elapsed_ms=1234),
            id=f"llm-{r.value}",
        )
        for r in LLMFailureReason
    ]
    cases += [
        pytest.param(_success(UNPARSEABLE[r]), id=f"parse-{r.value}")
        for r in RCAParseFailureReason
    ]
    return cases


async def _seed(db: Database) -> tuple[UUID, UUID, UUID]:
    """An incident with an alert, and its plan — as the pipeline leaves them."""
    correlation_id = uuid4()
    incident = Incident(
        id=uuid4(),
        correlation_id=correlation_id,
        fingerprint="f" * 64,
        service_name="order-service",
        title="order-service OrderProcessingFailureRate",
        severity="critical",
        status="open",
        alert_count=3,
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=correlation_id,
        steps=[
            {"order": 1, "description": "Check recent deployments"},
            {"order": 2, "description": "Review error logs"},
        ],
        template_key="order-service:OrderProcessingFailureRate",
        status="pending",
    )
    async with db.session() as session:
        session.add(incident)
        session.add(
            Alert(
                id=uuid4(),
                source="mock",
                fingerprint="f" * 64,
                service_name="order-service",
                alert_name="OrderProcessingFailureRate",
                severity="critical",
                status="firing",
                raw_payload={},
                fired_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                incident_id=incident.id,
                correlation_id=correlation_id,
            )
        )
        session.add(plan)
        await session.commit()
    return incident.id, plan.id, correlation_id


def _bundle(incident_id: UUID) -> ContextBundle:
    return ContextBundle(
        incident_id=incident_id,
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
        severity=Severity.CRITICAL,
        opened_at=datetime.now(UTC),
        alert_count=3,
        investigation_steps=[
            PlanStep(order=1, description="Check recent deployments"),
            PlanStep(order=2, description="Review error logs"),
        ],
        retrieved_context=[],
    )


async def _commit_outcome(
    db: Database,
    *,
    incident_id: UUID,
    plan_id: UUID,
    correlation_id: UUID,
    outcome: ReasoningOutcome,
    event_id: UUID | None = None,
) -> UUID:
    """The write path exactly as the handler runs it: store + marker + ONE commit."""
    async with db.session() as session:
        recommendation_id = await store_recommendation(
            session,
            correlation_id=correlation_id,
            incident_id=incident_id,
            plan_id=plan_id,
            outcome=outcome,
        )
        await mark_processed(session, event_id or new_id(), "reasoner-agent")
        await session.commit()
    return recommendation_id


async def _row(db: Database, recommendation_id: UUID) -> Recommendation:
    """Re-read from a FRESH session. The point: this is what Postgres actually kept."""
    async with db.session() as session:
        row = await session.get(Recommendation, recommendation_id)
    assert row is not None, "the recommendation did not survive the commit"
    return row


# --- THE INVARIANT AT THE ROW LEVEL -------------------------------------------


@pytest.mark.parametrize("result", _every_failing_result())
async def test_every_trigger_becomes_a_persisted_fallback_row(
    db: Database, result: LLMResult
) -> None:
    """Each trigger → a recommendation ROW exists, after the commit, is_fallback=true.

    The row version of R6's totality proof. Re-read from a fresh session, so this is a
    claim about what Postgres kept — not about what an ORM identity map remembers.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), result)

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.is_fallback is True
    assert row.incident_id == incident_id
    assert row.plan_id == plan_id
    assert row.root_cause
    assert row.recommended_actions  # something for the engineer to DO
    assert row.confidence == Confidence.LOW.value
    # The correlation chain: the ingress value, all the way to the last row written.
    assert row.correlation_id == correlation_id


@pytest.mark.parametrize("result", _every_failing_result())
async def test_a_persisted_fallback_row_names_no_provider(
    db: Database, result: LLMResult
) -> None:
    """is_fallback ⟺ provider="none", against REAL COLUMNS this time.

    The object could only agree with itself by construction — ``resolve`` sets both
    fields in one place. The INSERT is a second mapping, written by hand, and it is
    where the two could drift apart without a single test noticing.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), result)

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.is_fallback is True
    assert row.llm_provider == FALLBACK_PROVIDER == "none"
    assert row.model_alias == "none"
    assert row.model_id == "template-fallback"


async def test_a_persisted_analysis_row_never_names_no_provider(db: Database) -> None:
    """The other direction, against real columns: is_fallback=false NEVER says "none".

    A real RCA that stored ``provider="none"`` would be filtered out of every dashboard
    that excludes fallback traffic — silently invisible, and indistinguishable from a
    template.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), _success(GOOD_RCA))

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.is_fallback is False
    assert row.llm_provider == "openai"
    assert row.llm_provider != FALLBACK_PROVIDER
    assert row.model_id == "gpt-4o"
    assert row.model_alias == LLMMode.EXTENDED.value
    assert row.confidence == Confidence.HIGH.value
    assert row.raw_llm_response == GOOD_RCA
    assert row.prompt_tokens == 420
    assert row.latency_ms == 8_500


@pytest.mark.parametrize(
    "result",
    [*_every_failing_result(), pytest.param(_success(GOOD_RCA), id="clean-success")],
)
async def test_the_stored_row_never_lies_about_itself(
    db: Database, result: LLMResult
) -> None:
    """The biconditional, over the whole space, asserted on what Postgres holds."""
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), result)

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.is_fallback == (row.llm_provider == FALLBACK_PROVIDER)


@pytest.mark.parametrize("reason", list(RCAParseFailureReason))
async def test_a_wasted_call_persists_what_it_cost(
    db: Database, reason: RCAParseFailureReason
) -> None:
    """The spend survives to the row. A template that cost 420 tokens says so."""
    incident_id, plan_id, correlation_id = await _seed(db)
    content = UNPARSEABLE[reason]
    outcome = resolve(_bundle(incident_id), _success(content))

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.is_fallback is True
    assert row.raw_llm_response == content  # the evidence, kept
    assert row.prompt_tokens == 420
    assert row.completion_tokens == 99
    assert row.latency_ms == 8_500


@pytest.mark.parametrize("reason", list(LLMFailureReason))
async def test_a_call_that_never_completed_persists_no_figures(
    db: Database, reason: LLMFailureReason
) -> None:
    """NULL in the column means "nothing ran" — a fact, not an editorial choice."""
    incident_id, plan_id, correlation_id = await _seed(db)
    failure = LLMFailure(reason=reason, detail="simulated", elapsed_ms=1234)
    outcome = resolve(_bundle(incident_id), failure)

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    assert row.raw_llm_response is None
    assert row.prompt_tokens is None
    assert row.completion_tokens is None
    assert row.latency_ms is None


@pytest.mark.parametrize("result", _every_failing_result())
async def test_the_stored_bundle_records_why_we_fell_back(
    db: Database, result: LLMResult
) -> None:
    """``fallback_reason`` and ``attempted_mode`` survive into the JSONB column.

    This is the only durable record of WHY a row is a template — the columns say a model
    did not answer, not what went wrong. It has to survive the round-trip through JSONB,
    which is a different question from whether the object had it.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), result)

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
    )

    row = await _row(db, recommendation_id)
    stored = row.context_bundle
    assert stored["fallback"]["reason"]
    assert stored["fallback"]["attempted_mode"] == LLMMode.EXTENDED.value
    # And the bundle the model was shown is nested VERBATIM beside it, not merged into
    # it — "what was sent" stays distinguishable from "what we added afterwards".
    assert stored["bundle"]["incident_id"] == str(incident_id)
    assert "fallback" not in stored["bundle"]


# --- ONE TRANSACTION: the row, the event, the audit, the marker ---------------


async def test_the_write_is_one_transaction(db: Database) -> None:
    """Recommendation + outbox event + audit + marker: all four, one commit."""
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), _success(GOOD_RCA))
    event_id = new_id()

    recommendation_id = await _commit_outcome(
        db,
        incident_id=incident_id,
        plan_id=plan_id,
        correlation_id=correlation_id,
        outcome=outcome,
        event_id=event_id,
    )

    async with db.session() as session:
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == RECOMMENDATION_CREATED_EVENT
            )
        )
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.entity_id == incident_id)
        )
        marker = await session.scalar(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(ProcessedEvent.event_id == event_id)
        )

    assert event is not None
    assert event.target_service == FEEDBACK_TARGET
    # The event NAMES the recommendation; it does not copy it. No root_cause here.
    assert event.payload == {
        "incident_id": str(incident_id),
        "recommendation_id": str(recommendation_id),
    }
    assert event.correlation_id == correlation_id

    assert audit is not None
    assert audit.payload["is_fallback"] is False
    assert audit.payload["recommendation_id"] == str(recommendation_id)

    assert marker == 1


async def test_nothing_commits_if_the_marker_fails(db: Database) -> None:
    """The marker lands WITH the recommendation, or nothing lands at all.

    The transaction boundary is the whole reason the two-transaction shape is safe. If
    the outbox event (or the row) could commit while the marker failed, a redelivery
    would write a SECOND recommendation and a second RCA card. If the marker could
    commit while the row failed, the incident would be marked handled with no RCA at
    all — the invariant defeated by a boundary rather than by a missing fallback.

    Mutation that must turn this red: commit the recommendation before writing the
    marker (two commits instead of one).
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = resolve(_bundle(incident_id), _success(GOOD_RCA))

    with pytest.raises(RuntimeError, match="boom"):
        async with db.session() as session:
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )
            # Whatever goes wrong between the write and the commit — here, a crash
            # standing in for the marker failing — must take ALL of it down.
            raise RuntimeError("boom")

    async with db.session() as session:
        recommendations = await session.scalar(
            select(func.count())
            .select_from(Recommendation)
            .where(Recommendation.incident_id == incident_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.event_type == RECOMMENDATION_CREATED_EVENT)
        )
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_id == incident_id)
        )

    assert recommendations == 0, "the recommendation survived a rolled-back transaction"
    assert events == 0, "the outbox event escaped the transaction that produced it"
    assert audits == 0
