"""One recommendation per incident — and three ways to violate it that look identical.

``recommendations`` has a unique index on ``incident_id`` and **TWO** deferred foreign
keys (``incident_id`` and ``plan_id``). The planner had one of each. So at COMMIT there
are three distinct ways to raise ``IntegrityError``:

    1. the unique index        -> a genuine RACE     -> absorb, 200
    2. incident_id FK          -> the incident is gone -> 422
    3. plan_id FK              -> the plan is gone     -> 422   (new: planner had none)

Same exception, same moment, three completely different meanings — and the duplicate
handler answers case 1 with "200, marked processed, nothing written". If cases 2 and 3
could reach it, a deleted row would be reported as success and the incident would lose
its RCA forever, silently, with the event marked handled.

The defence is ORDERING, not cleverness: both foreign keys are checked explicitly before
the insert, so by the time an ``IntegrityError`` can be raised at all, the only
constraint left to violate is the unique index. The tests below exist to prove that
ordering is load-bearing rather than decorative.

WHY THE DEFERRED TIMING IS TESTED DIRECTLY
------------------------------------------
The FKs are ``DEFERRABLE INITIALLY DEFERRED``, so a bad reference does NOT fail at
``INSERT`` — it fails at ``COMMIT``, which is exactly what makes it indistinguishable
from the race. ``test_the_foreign_keys_fail_at_commit_not_at_insert`` pins that timing
against real Postgres, because the entire design rests on it:

- If someone made the FKs immediate, the failure would move to the flush, the tests
  written against "it fails somewhere" would keep passing, and this suite would no
  longer be testing the thing it was written to test.
- A test that merely asserted "IntegrityError is raised eventually" would pass for the
  wrong reason and would never notice.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER, new_id
from radar_contracts import LLMMode, PlanStep, ReasoningRequestedPayload, Severity
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
from radar_reasoner_agent import storage
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.fallback import ReasoningOutcome, resolve
from radar_reasoner_agent.llm import LLMSuccess
from radar_reasoner_agent.main import create_app
from radar_reasoner_agent.storage import (
    IncidentNotFoundError,
    PlanNotFoundError,
    store_recommendation,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

GOOD_RCA = (
    '{"root_cause": "A bad deploy broke order validation.", "confidence": "high", '
    '"recommended_actions": [{"order": 1, "action": "kubectl rollout undo"}]}'
)

TOKEN = "r" * 64
UNREACHABLE_GATEWAY = "http://127.0.0.1:1"
"""Nothing listens there. Every /events call below therefore takes the FALLBACK path —
which is fine: the duplicate guards do not care how the RCA was produced, only that one
already exists."""


@pytest_asyncio.fixture
async def client(
    db: Database, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """Real app, real lifespan, real Postgres. The race must cross the HANDLER."""
    (tmp_path / "postgres_dsn").write_text(database_url)
    (tmp_path / "agent_token").write_text(TOKEN)
    (tmp_path / "gateway_token").write_text("g" * 64)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_GATEWAY_URL", UNREACHABLE_GATEWAY)

    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            yield http


def _envelope(incident_id: UUID, plan_id: UUID, correlation_id: UUID) -> dict[str, Any]:
    """A well-formed reasoning_requested envelope with a FRESH event_id.

    Fresh, because the duplicate the unique index catches is two DISTINCT events for one
    incident — the processed_events gate already handles a redelivery of the same event,
    and would mask the race entirely.
    """
    return {
        "event_id": str(new_id()),
        "event_type": "incident.reasoning_requested",
        "correlation_id": str(correlation_id),
        "payload": ReasoningRequestedPayload(
            incident_id=incident_id, plan_id=plan_id
        ).model_dump(mode="json"),
    }


async def _seed(db: Database) -> tuple[UUID, UUID, UUID]:
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
        steps=[{"order": 1, "description": "Check recent deployments"}],
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
        investigation_steps=[PlanStep(order=1, description="Check recent deployments")],
        retrieved_context=[],
    )


def _outcome(incident_id: UUID) -> ReasoningOutcome:
    return resolve(
        _bundle(incident_id),
        LLMSuccess(
            content=GOOD_RCA,
            provider="openai",
            model="gpt-4o",
            mode=LLMMode.EXTENDED.value,
            prompt_tokens=420,
            completion_tokens=99,
            latency_ms=8_500,
        ),
    )


async def _count(db: Database, model: Any, **where: Any) -> int:
    async with db.session() as session:
        stmt = select(func.count()).select_from(model)
        for column, value in where.items():
            stmt = stmt.where(getattr(model, column) == value)
        count = await session.scalar(stmt)
    return int(count or 0)


# --- THE PREMISE: the FKs really are deferred ---------------------------------


@pytest.mark.parametrize("broken", ["incident_id", "plan_id"])
async def test_the_foreign_keys_fail_at_commit_not_at_insert(
    db: Database, broken: str
) -> None:
    """Both FKs are DEFERRED: a bad reference survives the INSERT and dies at COMMIT.

    This is the premise the whole module rests on, so it is pinned against real Postgres
    rather than assumed from the model definition.

    The flush MUST succeed. That is the trap: the violation does not surface where the
    bad row is written, it surfaces at commit — at the same instant, and as the same
    exception, as the unique-index race. That is what makes an unguarded insert able to
    mistake a deleted incident for a duplicate and answer 200.

    If someone made either FK immediate, the flush below would raise and this test would
    fail — which is the point. A test that only asserted "IntegrityError eventually"
    would keep passing and would have stopped testing anything.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    async with db.session() as session:
        session.add(
            Recommendation(
                id=new_id(),
                incident_id=uuid4() if broken == "incident_id" else incident_id,
                plan_id=uuid4() if broken == "plan_id" else plan_id,
                correlation_id=correlation_id,
                root_cause=outcome.root_cause,
                confidence=outcome.confidence.value,
                recommended_actions=[
                    a.model_dump() for a in outcome.recommended_actions
                ],
                context_bundle=outcome.context_bundle.model_dump(mode="json"),
                is_fallback=outcome.is_fallback,
                llm_provider=outcome.llm_provider,
                model_alias=outcome.model_alias,
                model_id=outcome.model_id,
            )
        )

        # THE DEFERRED WINDOW. A row pointing at nothing, accepted without complaint.
        await session.flush()

        with pytest.raises(IntegrityError):
            await session.commit()


# --- FK source 2 and 3: the referenced row vanished ---------------------------


async def _purge_incident(db: Database, incident_id: UUID) -> None:
    """Delete an incident — WITH its alert and its plan, because it cannot go alone.

    ``investigation_plans.incident_id`` and ``alerts.incident_id`` are foreign keys, so
    an "incident deleted, plan still there" state is UNREACHABLE: Postgres refuses it.
    A vanished incident always means a purged incident *family* — a retention job, an
    operator cleaning up. That is the scenario, so that is what the test builds.
    """
    async with db.session() as session:
        for model in (Alert, InvestigationPlan):
            for row in await session.scalars(
                select(model).where(model.incident_id == incident_id)
            ):
                await session.delete(row)
        await session.delete(await session.get(Incident, incident_id))
        await session.commit()


async def test_a_vanished_incident_is_refused_and_never_absorbed_as_a_race(
    db: Database,
) -> None:
    """The incident was purged while the reasoner waited on the LLM.

    Raised BEFORE the insert, so it can never arrive at the caller wearing the race's
    clothes. Mutation that must turn this red: delete the incident existence check —
    the insert then reaches commit, the deferred FK fires ``IntegrityError``, the
    duplicate handler absorbs it, and the event is lost with a 200.

    Note the incident check is what makes the DIAGNOSIS right, not merely the refusal:
    a purge takes the plan too, so without this check the plan check would fire and the
    operator would be sent looking for a missing plan when the entire incident is gone.
    Both errors refuse the write; only one of them says the true thing.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    # The window: the LLM call took 30 seconds, and the incident was purged.
    await _purge_incident(db, incident_id)

    async with db.session() as session:
        with pytest.raises(IncidentNotFoundError):
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )


async def test_a_vanished_plan_is_refused_and_never_absorbed_as_a_race(
    db: Database,
) -> None:
    """The SECOND foreign key — the door the planner never had.

    The incident still exists, so the incident check passes and would have been
    "sufficient" by the planner's standard. The plan is gone, and without its own check
    the deferred ``plan_id`` FK fires at commit, is caught by the duplicate handler, and
    the incident silently loses its RCA.

    Mutation that must turn this red: delete the plan existence check.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    async with db.session() as session:
        await session.delete(await session.get(InvestigationPlan, plan_id))
        await session.commit()

    async with db.session() as session:
        # The incident is STILL THERE — this is not the incident check firing.
        assert await session.get(Incident, incident_id) is not None

        with pytest.raises(PlanNotFoundError):
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )


async def test_the_two_missing_row_errors_are_distinguishable(db: Database) -> None:
    """A missing incident and a missing plan are NOT collapsed into "some FK is gone".

    They point at different upstream bugs, and the operator woken at 3am needs to know
    which row to go looking for. Same principle as ``rejected`` vs
    ``gateway_unavailable``: distinguish causes that demand different human responses.
    """
    assert not issubclass(IncidentNotFoundError, PlanNotFoundError)
    assert not issubclass(PlanNotFoundError, IncidentNotFoundError)

    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    async with db.session() as session:
        await session.delete(await session.get(InvestigationPlan, plan_id))
        await session.commit()

    async with db.session() as session:
        with pytest.raises(PlanNotFoundError) as caught:
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )

    # The message NAMES the plan, and names the id the operator has to go find.
    message = str(caught.value)
    assert "plan" in message.lower()
    assert str(plan_id) in message


@pytest.mark.parametrize("vanished", ["incident", "plan"])
async def test_a_refused_write_leaves_nothing_behind(
    db: Database, vanished: str
) -> None:
    """Nothing is written, and NO MARKER — so the event is dead-lettered, not lost.

    The whole danger of absorbing these as duplicates is that the marker gets written.
    A refused write must leave the event redeliverable and visible to a human.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)
    event_id = new_id()

    if vanished == "incident":
        await _purge_incident(db, incident_id)
    else:
        async with db.session() as session:
            await session.delete(await session.get(InvestigationPlan, plan_id))
            await session.commit()

    with pytest.raises(storage.ReferencedRowMissingError):
        async with db.session() as session:
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )
            await mark_processed(session, event_id, "reasoner-agent")
            await session.commit()

    assert await _count(db, Recommendation) == 0
    assert await _count(db, ProcessedEvent, event_id=event_id) == 0, (
        "a marker was written for an event that produced nothing — it is now lost"
    )
    assert await _count(db, OutboxEvent, event_type="recommendation.created") == 0


# --- FK source 1: the genuine race, which IS safe to absorb -------------------


async def test_the_sequential_duplicate_is_caught_by_the_precheck(db: Database) -> None:
    """A second reasoning_requested for an already-recommended incident: no-op, audited.

    The pre-check finds the existing recommendation and does not insert. The incident IS
    recommended, so re-reasoning is unnecessary rather than erroneous — but absorbing an
    upstream bug in silence is how it stays a bug, so it leaves an audit row.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    async with db.session() as session:
        first = await store_recommendation(
            session,
            correlation_id=correlation_id,
            incident_id=incident_id,
            plan_id=plan_id,
            outcome=outcome,
        )
        await mark_processed(session, new_id(), "reasoner-agent")
        await session.commit()

    async with db.session() as session:
        second = await store_recommendation(
            session,
            correlation_id=correlation_id,
            incident_id=incident_id,
            plan_id=plan_id,
            outcome=outcome,
        )
        await mark_processed(session, new_id(), "reasoner-agent")
        await session.commit()

    assert second.duplicate is True
    assert second.recommendation_id == first.recommendation_id
    assert await _count(db, Recommendation, incident_id=incident_id) == 1
    # Exactly ONE recommendation.created event — the duplicate emitted none.
    assert await _count(db, OutboxEvent, event_type="recommendation.created") == 1
    assert (
        await _count(db, AuditLog, event_type=storage.AUDIT_DUPLICATE_IGNORED) == 1
    ), "the ignored duplicate left no audit trail"


async def test_a_concurrent_duplicate_loses_to_the_unique_index(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE BACKSTOP, actually raced — not merely asserted to exist.

    The pre-check cannot help here: a barrier is installed BETWEEN the pre-check and the
    insert, so both writers provably see "no existing recommendation" before either
    inserts. One commits; the other is rejected by
    ``idx_recommendations_one_per_incident`` with ``IntegrityError``.

    This is the exception the handler absorbs as a 200 — and it is the ONLY one that can
    still reach it, because both foreign keys were ruled out above. That is the whole
    argument of this module, and this test is the half of it that proves the race real.

    Mutation that must turn this red: remove the ``try``/``except IntegrityError`` in
    the handler — the race then escapes as an unhandled 500.
    """
    incident_id, plan_id, correlation_id = await _seed(db)
    outcome = _outcome(incident_id)

    barrier = asyncio.Barrier(2)
    real_lookup = storage._existing_recommendation_id

    async def gated(session: Any, incident: UUID) -> UUID | None:
        found = await real_lookup(session, incident)
        # Neither writer has inserted yet. Both saw nothing. Now both proceed.
        await barrier.wait()
        return found

    monkeypatch.setattr(storage, "_existing_recommendation_id", gated)

    async def write() -> None:
        async with db.session() as session:
            await store_recommendation(
                session,
                correlation_id=correlation_id,
                incident_id=incident_id,
                plan_id=plan_id,
                outcome=outcome,
            )
            await mark_processed(session, new_id(), "reasoner-agent")
            await session.commit()

    results = await asyncio.gather(write(), write(), return_exceptions=True)

    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1, "both writers succeeded — the unique index did not fire"
    assert isinstance(failures[0], IntegrityError), (
        f"the loser failed with {type(failures[0]).__name__}, not IntegrityError"
    )

    # One incident, one recommendation. The index held.
    assert await _count(db, Recommendation, incident_id=incident_id) == 1


async def test_the_handler_absorbs_the_race_instead_of_500ing(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race, driven through the HANDLER — because that is where it is absorbed.

    Mutation testing found this gap and it is worth naming: the test above proves the
    unique index fires, but it calls ``store_recommendation`` directly. Deleting the
    handler's entire ``except IntegrityError`` block changed NOTHING in the suite — the
    absorb-and-200 path was, as far as the tests could tell, dead code kept alive by a
    docstring. That is precisely the failure mode the backstop is most prone to.

    So the race is forced through two concurrent ``POST /events`` calls. The loser must
    come back **200**, not 500: the other transaction wrote the RCA, so the work IS done
    and a 500 would only have the worker retry a race it would lose again. And the loser
    MUST leave a marker behind, or the worker redelivers it forever.
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    barrier = asyncio.Barrier(2)
    real_lookup = storage._existing_recommendation_id

    async def gated(session: Any, incident: UUID) -> UUID | None:
        found = await real_lookup(session, incident)
        await barrier.wait()  # neither delivery has inserted yet
        return found

    monkeypatch.setattr(storage, "_existing_recommendation_id", gated)

    # Two DISTINCT deliveries — different event_ids, same incident. The processed_events
    # gate cannot help here: it keys on event_id, and these are two different events.
    bodies = [_envelope(incident_id, plan_id, correlation_id) for _ in range(2)]
    responses = await asyncio.gather(
        *(
            client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})
            for body in bodies
        )
    )

    assert [r.status_code for r in responses] == [200, 200], (
        "the loser of the race did not get a 200 — the backstop let it escape"
    )
    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["already_recommended", "processed"]

    # ONE recommendation, ONE outbox event: the loser wrote neither.
    assert await _count(db, Recommendation, incident_id=incident_id) == 1
    assert await _count(db, OutboxEvent, event_type="recommendation.created") == 1
    # BOTH events marked — the loser must not be redelivered forever.
    assert await _count(db, ProcessedEvent) == 2
    # And the absorbed duplicate is on the record, not swallowed in silence.
    assert await _count(db, AuditLog, event_type=storage.AUDIT_DUPLICATE_IGNORED) == 1
