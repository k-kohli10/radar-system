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

import json
from uuid import UUID

from radar_database import (
    AuditLog,
    Feedback,
    Incident,
    InvestigationPlan,
    OutboxEvent,
    Recommendation,
)
from sqlalchemy import func, select

from tests.e2e.harness import FEEDBACK_SERVICE, Pipeline, correlation_ids

#: RADAR's own action id for the 👍 button, echoed back on a click. Hard-coded rather
#: than imported so a rename in the service is caught HERE — the id is a wire value the
#: Slack payload carries, and importing the enum would make the test agree with any
#: rename automatically, including one that breaks every card already in a channel.
FEEDBACK_UP = "feedback.up"


def _counter(metrics: str, sentiment: str) -> float:
    """``radar_feedback_total`` for one sentiment, 0.0 before it is first incremented.

    A Prometheus counter with labels emits no sample until a label combination is used,
    so absent means zero here — but only for this exact family/label pair, so a typo in
    either would read as a flat zero. The test asserts a DELTA across a known increment,
    which is what makes that failure mode visible.
    """
    needle = f'radar_feedback_total{{sentiment="{sentiment}"}} '
    for line in metrics.splitlines():
        if line.startswith(needle):
            return float(line[len(needle) :])
    return 0.0


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


async def test_a_duplicate_alert_produces_no_second_rca(pipeline: Pipeline) -> None:
    """Two identical alerts → ONE incident, ONE RCA. Deduplication, end to end.

    The plan lists this as an e2e case, and it is a different claim from the ingestion
    suite's dedup-window tests. Ingestion attaches the second alert to the open incident
    (same fingerprint, inside the 5-minute window) and bumps ``alert_count`` — proven at
    that boundary already. What only the *whole pipeline* can show is that the duplicate
    does not FAN OUT: no second plan, no second (paid) LLM call, no second RCA
    contradicting the first. The planner and reasoner each dedup their own hand-off, so
    the one-RCA-per-incident guarantee holds across every stage, not just at the door.

    Dedup is not drop: both alerts land on the incident (``alert_count == 2``), so the
    watcher still sees the burst it needs for escalation — the second alert is absorbed,
    not discarded.
    """
    first = await pipeline.post_alert()
    second = await pipeline.post_alert()  # same MOCK_ALERT → same fingerprint
    assert first.status_code == second.status_code == 202
    # Ingestion attached the second alert; it did not open a second incident.
    assert first.json()["incident_id"] == second.json()["incident_id"]

    await pipeline.drain()

    async with pipeline.db.session() as session:
        incident = (await session.scalars(select(Incident))).one()  # .one() ⇒ exactly 1
        plans = await session.scalar(
            select(func.count()).select_from(InvestigationPlan)
        )
        recs = await session.scalar(select(func.count()).select_from(Recommendation))

    assert plans == 1, "the duplicate spawned a second investigation plan"
    assert recs == 1, "the duplicate spawned a second, contradictory recommendation"
    assert incident.alert_count == 2, (
        "dedup dropped the second alert instead of attaching it"
    )


async def test_the_whole_outbox_drains_with_nothing_left_behind(
    pipeline: Pipeline,
) -> None:
    """Every hand-off is consumed, including the last one. Nothing dead-letters.

    The outbox is a queue, not a log: ``mark_dispatched`` DELETES a delivered event,
    so a fully-walked pipeline leaves the table EMPTY — all four hand-offs
    (``alert.normalized``, ``incident.plan_requested``,
    ``incident.reasoning_requested``, ``recommendation.created``) gone, each consumed
    by the stage that handled it.

    Until Phase 9 this test asserted the opposite for the last event:
    ``feedback-service`` did not exist, so ``recommendation.created`` dead-lettered and
    one row stayed behind. An empty table is now the correct terminal state, and the
    dead-letter MECHANISM is proven where it belongs — against the worker, in
    ``apps/outbox-worker/tests/test_dead_letter_promotion.py``.
    """
    await pipeline.post_alert()
    await pipeline.drain()

    async with pipeline.db.session() as session:
        remaining = list(await session.scalars(select(OutboxEvent)))

    assert remaining == [], (
        "the outbox did not fully drain; left behind: "
        f"{[(e.event_type, e.status) for e in remaining]}"
    )


async def test_alert_to_card_to_feedback_row(pipeline: Pipeline) -> None:
    """Phase 9's done-condition #1, end to end: alert → RCA card → 👍 → feedback row.

    The first place the whole loop exists at once. Every stage is real — ingestion,
    the watcher, the planner, the reasoner, the outbox-worker, feedback-service, and
    Postgres — with only the LLM provider and the Slack TRANSPORT substituted. In
    particular the card is built by the real formatter and the click is handled by the
    real interaction handler, reached through the vendor-neutral contract the Slack
    plugin uses.

    Four claims, and each would hide a different broken seam if it were dropped:

    1. ``recommendation.created`` was DELIVERED, not dead-lettered — the worker reached
       feedback-service. Asserted as the delivered card, because a delivered event that
       posted nothing is the failure this is really guarding.
    2. The incident moved ``open -> investigating`` ON delivery, so ``investigating``
       means "a human has been told" (ADR 0016 Amendment 1).
    3. A 👍 writes ONE feedback row, linked to the right recommendation AND the right
       incident, with the sentiment the contract names.
    4. ``radar_feedback_total`` moved. The counter is incremented after the row commits,
       so a dashboard reading it is reading recorded feedback, not attempts.
    """
    response = await pipeline.post_alert()
    assert response.status_code == 202
    incident_id = UUID(response.json()["incident_id"])

    await pipeline.drain()

    async with pipeline.db.session() as session:
        rec = (await session.scalars(select(Recommendation))).one()

    # (1) The card the on-call engineer would have seen, from a delivered event.
    card = pipeline.slack.card_for(rec.id)
    assert len(pipeline.slack.notifier.sent) == 1, "exactly one card per recommendation"
    assert rec.root_cause in json.dumps(card["blocks"]), (
        "the delivered card does not carry this recommendation's root cause"
    )

    # (2) Delivery is what moves the incident, not the recommendation being written.
    async with pipeline.db.session() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        assert incident.status == "investigating", (
            f"delivery did not transition the incident; status={incident.status!r}"
        )

    before = _counter(await pipeline.scrape(FEEDBACK_SERVICE), "helpful")

    # (3) The 👍, through the real handler.
    await pipeline.click(FEEDBACK_UP, recommendation_id=rec.id)

    async with pipeline.db.session() as session:
        feedback = (await session.scalars(select(Feedback))).one()

    assert feedback.recommendation_id == rec.id, "feedback linked to the wrong RCA"
    assert feedback.incident_id == incident_id, "feedback linked to the wrong incident"
    assert feedback.sentiment == "helpful"

    # (4) And the counter moved, after the row committed.
    after = _counter(await pipeline.scrape(FEEDBACK_SERVICE), "helpful")
    assert after == before + 1, (
        f"radar_feedback_total{{sentiment=helpful}} went {before} -> {after}"
    )


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

    # Every audit row carries it too — not just the entity rows.
    audit_ids = await correlation_ids(pipeline.db, AuditLog)
    assert audit_ids, "no AuditLog rows were written"
    assert set(audit_ids) == {ingress}, (
        "AuditLog carries a correlation id the pipeline did not mint"
    )

    # And every outbox EVENT, read from what the drain claimed rather than from the
    # table. The outbox is a queue: a delivered event is deleted, so now that the whole
    # pipeline drains there is no row left to inspect. This is strictly more coverage
    # than querying it used to give — all four hand-offs, where a post-drain SELECT only
    # ever saw the one event that failed to deliver.
    # The COUNT is asserted, not just the values: `set(...) == {ingress}` holds just as
    # well for one recorded id as for four, so on its own it would keep passing if a hop
    # stopped emitting and the chain silently got shorter — the regression this test
    # exists to catch. Four hand-offs, and if a fifth is ever added this fails and says
    # so rather than quietly checking three of them.
    assert len(pipeline.dispatched_correlation_ids) == 4, (
        "expected the four hand-offs (alert.normalized, incident.plan_requested, "
        "incident.reasoning_requested, recommendation.created); got "
        f"{len(pipeline.dispatched_correlation_ids)}"
    )
    assert set(pipeline.dispatched_correlation_ids) == {ingress}, (
        "an outbox event carried a correlation id the pipeline did not mint"
    )


async def test_the_reasoner_actually_called_the_gateway(pipeline: Pipeline) -> None:
    """The recommendation came from the LLM path, not a fallback that never dialed out.

    Guards a false green: a reasoner that fell back on every incident would still
    produce a recommendation and pass the row assertions above. The proof that the
    SUCCESS path ran is that the gateway received the call.
    """
    await pipeline.post_alert()
    await pipeline.drain()

    assert len(pipeline.mock.received) == 1
    request = pipeline.mock.received[0]
    assert request["mode"] == "extended"
    # The reasoner sends the system prompt + the context bundle as the user message.
    assert any(m["role"] == "system" for m in request["messages"])
