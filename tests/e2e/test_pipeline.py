"""E1: one alert in, one recommendation out — and one correlation id on every row.

The first proof that the four services compose into a pipeline. Everything upstream has
been tested one service at a time; this asserts the seams between them hold, driven
end-to-end through the real outbox-worker with a mocked LLM.

The load-bearing assertion is the correlation chain. Phase 10 will trace an incident by
the single id minted at ingress, and that only works if every stage writes THAT id and
never a fresh one. No unit test can prove it — each sees only its own row. This is the
first place the whole chain exists at once.
"""

from __future__ import annotations

from radar_database import (
    STATUS_DEAD_LETTER,
    AuditLog,
    Incident,
    InvestigationPlan,
    OutboxEvent,
    Recommendation,
)
from sqlalchemy import func, select

from tests.e2e.harness import Pipeline, correlation_ids


async def test_alert_becomes_a_recommendation(pipeline: Pipeline) -> None:
    """Front door to finished RCA: 202 → drain → exactly one recommendation row."""
    response = await pipeline.post_alert()
    assert response.status_code == 202
    incident_id = response.json()["incident_id"]

    await pipeline.drain()

    async with pipeline.db.session() as session:
        recommendations = list(await session.scalars(select(Recommendation)))
    assert len(recommendations) == 1
    rec = recommendations[0]
    assert str(rec.incident_id) == incident_id
    # The mock gateway answered, so this is a REAL analysis, not a fallback.
    assert rec.is_fallback is False
    assert rec.llm_provider == "mock-openai"
    assert rec.model_id == "mock-gpt-4o"
    assert rec.confidence == "high"
    assert rec.recommended_actions[0]["action"].startswith("kubectl rollout undo")


async def test_the_pipeline_wrote_one_of_each_row(pipeline: Pipeline) -> None:
    """Each stage left exactly its own row — no stage ran twice, none was skipped."""
    await pipeline.post_alert()
    await pipeline.drain()

    async with pipeline.db.session() as session:
        for table in (Incident, InvestigationPlan, Recommendation):
            n = await session.scalar(select(func.count()).select_from(table))
            assert n == 1, f"expected one {table.__name__}, got {n}"


async def test_recommendation_created_dead_letters_until_phase_9(
    pipeline: Pipeline,
) -> None:
    """The hand-off events are consumed; the one with no consumer yet dead-letters.

    The outbox is a queue, not a log: ``mark_dispatched`` DELETES a delivered event, so
    after a full drain the three intermediate hand-offs (``alert.normalized``,
    ``incident.plan_requested``, ``incident.reasoning_requested``) are gone — consumed
    by the stage that handled them. That they were emitted and delivered is proven by
    the rows they produced (the previous test).

    The one event left is ``recommendation.created``. Its target,
    ``feedback-service``, does not exist until Phase 9 and has no dispatch token, so the
    worker dead-letters it rather than delivering it. That is the correct, visible
    boundary of the POC — and the recommendation ROW is already durably written, which
    is what the pipeline is for.
    """
    await pipeline.post_alert()
    await pipeline.drain()

    async with pipeline.db.session() as session:
        remaining = list(await session.scalars(select(OutboxEvent)))

    assert len(remaining) == 1, "delivered hand-offs should have been consumed"
    assert remaining[0].event_type == "recommendation.created"
    assert remaining[0].status == STATUS_DEAD_LETTER


async def test_one_correlation_id_runs_through_every_row(pipeline: Pipeline) -> None:
    """THE traceability invariant: one ingress id on every row the pipeline writes.

    The id minted by ingestion appears — unchanged — on the incident, the plan, the
    recommendation, every audit row, and every outbox event. This is the property Phase
    10 traces an incident by, and the first test in which the entire chain is visible at
    once. A stage that minted a fresh id would break the trace here and nowhere else.
    """
    await pipeline.post_alert()
    await pipeline.drain()

    async with pipeline.db.session() as session:
        incident = (await session.scalars(select(Incident))).one()
        plan = (await session.scalars(select(InvestigationPlan))).one()
        rec = (await session.scalars(select(Recommendation))).one()

    ingress = incident.correlation_id  # the value ingestion minted
    assert plan.correlation_id == ingress
    assert rec.correlation_id == ingress

    # Every audit row and every outbox event carries it too — not just the entity rows.
    for table in (AuditLog, OutboxEvent):
        ids = await correlation_ids(pipeline.db, table)
        assert ids, f"no {table.__name__} rows were written"
        assert set(ids) == {ingress}, (
            f"{table.__name__} carries a correlation id the pipeline did not mint"
        )


async def test_the_reasoner_actually_called_the_gateway(pipeline: Pipeline) -> None:
    """The recommendation came from the LLM path, not a fallback that never dialed out.

    Guards a false green: a reasoner that fell back on every incident would still
    produce a recommendation and pass the row assertions above. The proof that the
    SUCCESS path ran is that the gateway received the call.
    """
    await pipeline.post_alert()
    await pipeline.drain()

    assert len(pipeline.gateway.received) == 1
    request = pipeline.gateway.received[0]
    assert request["mode"] == "extended"
    # The reasoner sends the system prompt + the context bundle as the user message.
    assert any(m["role"] == "system" for m in request["messages"])
