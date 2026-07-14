"""Plan storage, the reasoning_requested trigger, and the duplicate-plan guard.

Three properties, against a real Postgres:

**The correlation chain.** Every row is asserted equal to the INGRESS uuid itself —
not merely equal to each other. A planner that minted a fresh id would produce rows
that are perfectly self-consistent and still sever the trace.

**One transaction.** The plan, the outbox event, the audit row, and the marker
commit together or not at all. A marker without its plan would be terminal *and*
silent: the gate would skip the redelivery, and that incident would never be planned
again.

**The duplicate-plan guard, INCLUDING the concurrent path.**

That last one is the point of this file. ``investigation_plans`` has a unique index
on ``incident_id``, and the idempotency gate does not protect it — the gate keys on
``event_id``, and a second *distinct* ``plan_requested`` for the same incident is a
different event. Sequentially the pre-check catches it. Concurrently, only the index
does.

The concurrent path is exactly the kind of backstop that gets written, asserted in a
docstring, and never actually executed — a code branch that looks defended and is
not. So it is forced: ``test_a_concurrent_duplicate_loses_to_the_unique_index``
installs a barrier BETWEEN the pre-check and the insert, so both deliveries provably
see "no plan yet" and both try to insert. One wins; the other must be caught, rolled
back, re-marked in a fresh transaction, and answered 200 — with no 500 escaping and
no second reasoning_requested. Remove the ``try`` around the insert and that test
fails with an unhandled IntegrityError, which is the proof the guard is load-bearing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import PlanRequestedPayload, PlanStep, ReasoningRequestedPayload
from radar_database import (
    AuditLog,
    Database,
    Incident,
    InvestigationPlan,
    OutboxEvent,
    ProcessedEvent,
    mark_processed,
)
from radar_planner_agent import planning
from radar_planner_agent.config import SERVICE_NAME
from radar_planner_agent.main import create_app
from radar_planner_agent.planning import (
    AUDIT_DUPLICATE_IGNORED,
    AUDIT_PLAN_CREATED,
    REASONER_TARGET,
    REASONING_REQUESTED_EVENT,
    IncidentNotFoundError,
    plan_incident,
)
from radar_planner_agent.templates import DEFAULT_KEY, load_plan_templates
from sqlalchemy import func, select

TOKEN = "p" * 64
SERVICE = "order-service"
ALERT = "OrderProcessingFailureRate"
TEMPLATES = load_plan_templates(Path("apps/planner-agent/config/plan-templates.yaml"))


class _InducedError(Exception):
    """Stands in for any failure between the writes and the commit."""


@pytest_asyncio.fixture
async def client(
    db: Database, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "postgres_dsn").write_text(database_url)
    (tmp_path / "agent_token").write_text(TOKEN)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))
    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://planner"
        ) as http:
            yield http


async def _seed_incident(db: Database, ingress: UUID) -> UUID:
    incident = Incident(
        id=uuid4(),
        correlation_id=ingress,
        fingerprint="f" * 64,
        service_name=SERVICE,
        title=f"{SERVICE} {ALERT}",
        severity="high",
        status="open",
        alert_count=1,
    )
    async with db.session() as session:
        session.add(incident)
        await session.commit()
    return incident.id


def _payload(
    incident_id: UUID, *, service_name: str = SERVICE, alert_name: str = ALERT
) -> PlanRequestedPayload:
    return PlanRequestedPayload(
        incident_id=incident_id, service_name=service_name, alert_name=alert_name
    )


def _envelope(payload: PlanRequestedPayload, ingress: UUID) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": "incident.plan_requested",
        "correlation_id": str(ingress),
        "payload": payload.model_dump(mode="json"),
    }


async def _post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})


async def _count(db: Database, model: Any) -> int:
    async with db.session() as session:
        n = await session.scalar(select(func.count()).select_from(model))
    return int(n or 0)


async def _plans(db: Database) -> list[InvestigationPlan]:
    async with db.session() as session:
        rows = (await session.execute(select(InvestigationPlan))).scalars().all()
    return list(rows)


async def _reasoning_events(db: Database) -> list[OutboxEvent]:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == REASONING_REQUESTED_EVENT
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _audit_types(db: Database) -> list[str]:
    async with db.session() as session:
        rows = (await session.execute(select(AuditLog.event_type))).scalars().all()
    return list(rows)


# --- the happy path: the RIGHT plan, stored and handed on ----------------------


async def test_a_plan_is_stored_and_reasoning_is_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    response = await _post(client, _envelope(_payload(incident_id), ingress))

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}

    plans = await _plans(db)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.incident_id == incident_id
    assert plan.template_key == f"{SERVICE}:{ALERT}", "the SPECIFIC template"
    assert plan.status == "pending"

    # The stored steps ARE the PlanStep contract shape, so the reasoner and the
    # Slack card read them without a translation layer. Validated, not eyeballed.
    steps = [PlanStep.model_validate(s) for s in plan.steps]
    assert [s.order for s in steps] == [1, 2, 3, 4, 5]
    assert "kubectl rollout history deployment/order-service" in steps[0].description

    events = await _reasoning_events(db)
    assert len(events) == 1
    assert events[0].target_service == REASONER_TARGET
    body = ReasoningRequestedPayload.model_validate(events[0].payload)
    assert body.incident_id == incident_id
    assert body.plan_id == plan.id


async def test_reasoning_requested_carries_no_mutable_incident_state(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Two ids, nothing else. The reasoner reads the ROWS, which are current.

    An incident can escalate between the plan being stored and the reasoner running.
    A severity copied into this payload would be frozen at write time, and the RCA
    card would tell an engineer the incident is 'high' while it is critical. Leaving
    the fields out makes that unrepresentable rather than merely discouraged.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    await _post(client, _envelope(_payload(incident_id), ingress))

    event = (await _reasoning_events(db))[0]
    assert set(event.payload) == {"incident_id", "plan_id"}
    for mutable in ("severity", "status", "alert_count", "steps"):
        assert mutable not in event.payload


async def test_an_unknown_alert_is_planned_from_the_default_template(
    client: httpx.AsyncClient, db: Database
) -> None:
    """No template, still an investigation. No incident is left unplanned."""
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    payload = _payload(
        incident_id, service_name="payment-gateway", alert_name="NobodyPlannedForThis"
    )

    await _post(client, _envelope(payload, ingress))

    plans = await _plans(db)
    assert len(plans) == 1
    assert plans[0].template_key == DEFAULT_KEY
    assert len(await _reasoning_events(db)) == 1, "the pipeline still moves"


# --- correlation-id threading ---------------------------------------------------


async def test_correlation_id_threads_from_ingress_through_reasoning_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Anchored to the ingress value ITSELF, not merely to internal consistency."""
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    await _post(client, _envelope(_payload(incident_id), ingress))

    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert incident is not None
    plan = (await _plans(db))[0]
    event = (await _reasoning_events(db))[0]

    assert incident.correlation_id == ingress, "the watcher's incident row"
    assert plan.correlation_id == ingress, "the planner's plan row"
    assert audits[0].correlation_id == ingress, "the planner's audit row"
    assert event.correlation_id == ingress, "the reasoning_requested event"

    assert (
        ingress
        == incident.correlation_id
        == plan.correlation_id
        == audits[0].correlation_id
        == event.correlation_id
    )


async def test_the_planner_never_mints_a_correlation_id(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Across everything the planner wrote, exactly ONE correlation id exists."""
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    await _post(client, _envelope(_payload(incident_id), ingress))

    async with db.session() as session:
        plan_ids = set(
            (await session.execute(select(InvestigationPlan.correlation_id)))
            .scalars()
            .all()
        )
        event_ids = set(
            (await session.execute(select(OutboxEvent.correlation_id))).scalars().all()
        )
        audit_ids = set(
            (await session.execute(select(AuditLog.correlation_id))).scalars().all()
        )

    assert plan_ids == event_ids == audit_ids == {ingress}


# --- the transaction boundary ----------------------------------------------------


async def test_plan_event_audit_and_marker_commit_together(db: Database) -> None:
    """All four land, in one transaction, or none of them do.

    Driven at the session level so the failure can be induced AFTER the rows have
    genuinely reached the database and BEFORE the commit — the only window where a
    split boundary is observable.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    event_id = uuid4()

    with pytest.raises(_InducedError):
        async with db.session() as session:
            await plan_incident(
                session,
                templates=TEMPLATES,
                correlation_id=ingress,
                payload=_payload(incident_id),
            )
            await mark_processed(session, event_id, SERVICE_NAME)
            await session.flush()
            raise _InducedError

    assert await _count(db, InvestigationPlan) == 0
    assert await _count(db, OutboxEvent) == 0
    assert await _count(db, AuditLog) == 0
    assert await _count(db, ProcessedEvent) == 0
    assert await _count(db, Incident) == 1, "the seeded incident is untouched"


# --- idempotency -----------------------------------------------------------------


async def test_redelivery_creates_no_second_plan(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The SAME event twice: the gate catches it before any work."""
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    body = _envelope(_payload(incident_id), ingress)

    first = await _post(client, body)
    second = await _post(client, body)

    assert first.json() == {"status": "processed"}
    assert second.json() == {"status": "already_processed"}
    assert await _count(db, InvestigationPlan) == 1
    assert len(await _reasoning_events(db)) == 1
    assert await _count(db, ProcessedEvent) == 1


# --- the duplicate-plan guard: SEQUENTIAL ----------------------------------------


async def test_a_second_distinct_event_for_a_planned_incident_is_absorbed(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A DIFFERENT event for an already-planned incident. The gate cannot catch it.

    Different event_id, so processed_events says nothing. The pre-check finds the
    existing plan and the planner no-ops: 200, one plan, ONE reasoning_requested. A
    second would mean a second investigation, a second LLM call, and two RCA cards
    for one incident.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    first = await _post(client, _envelope(_payload(incident_id), ingress))
    second = await _post(client, _envelope(_payload(incident_id), ingress))

    assert first.json() == {"status": "processed"}
    assert second.json() == {"status": "already_planned"}
    assert second.status_code == 200, "never a 500 — the work is already done"

    assert await _count(db, InvestigationPlan) == 1
    assert len(await _reasoning_events(db)) == 1
    assert await _count(db, ProcessedEvent) == 2, "both events were handled"
    # The absorbed duplicate is not silent: it is in the audit trail.
    assert AUDIT_DUPLICATE_IGNORED in await _audit_types(db)


# --- the duplicate-plan guard: CONCURRENT (the real backstop) ---------------------


async def test_a_concurrent_duplicate_loses_to_the_unique_index(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE test. Two deliveries interleave; the unique index rejects the loser.

    The pre-check cannot help here: a barrier is installed BETWEEN the pre-check and
    the insert, so both requests provably see "no plan yet" and both proceed to
    insert. One commits; the other's INSERT violates
    ``idx_plans_one_per_incident`` and raises IntegrityError.

    That is the path a sequential test can never reach, and the one a backstop
    quietly fails to have. What must hold:

      - exactly ONE investigation_plan
      - exactly ONE reasoning_requested (or the reasoner runs the LLM twice)
      - BOTH requests answered 200 — no 500 escaped to the worker, which would
        retry a race it will simply lose again
      - BOTH events marked processed — the loser's marker written in a FRESH
        transaction, since its original was rolled back with the failed insert
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    # Force the interleave: both requests complete their pre-check before either
    # inserts. Without this the second request would simply see the first's plan and
    # take the (easy) sequential path, leaving the index backstop unexercised.
    barrier = asyncio.Barrier(2)
    original = planning._existing_plan_id

    async def gated(session: Any, incident: UUID) -> UUID | None:
        result = await original(session, incident)
        await barrier.wait()  # neither side has inserted yet
        return result

    monkeypatch.setattr(planning, "_existing_plan_id", gated)

    first, second = await asyncio.gather(
        _post(client, _envelope(_payload(incident_id), ingress)),
        _post(client, _envelope(_payload(incident_id), ingress)),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 200], "no 500 escaped — the race is not an error"

    outcomes = sorted([first.json()["status"], second.json()["status"]])
    assert outcomes == ["already_planned", "processed"], (
        "one delivery planned the incident; the other must have been absorbed"
    )

    assert await _count(db, InvestigationPlan) == 1, "the unique index held"
    assert len(await _reasoning_events(db)) == 1, "the reasoner is asked exactly once"
    assert await _count(db, ProcessedEvent) == 2, (
        "the loser's marker was rewritten in a fresh transaction — without it the "
        "worker would redeliver forever, losing the same race every time"
    )
    types = await _audit_types(db)
    assert AUDIT_PLAN_CREATED in types
    assert AUDIT_DUPLICATE_IGNORED in types, "the lost race is in the audit trail"


async def test_the_duplicate_counter_and_default_counter_are_exported(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The two counters that make silent bugs loud, scraped from /metrics.

    ``radar_plans_created_total{outcome="default"}`` is the production analog of the
    round-trip key test: CI catches a template key that drifted in the repo, but a
    climbing default rate is the ONLY symptom of one that drifted in a real
    deployment. ``radar_duplicate_plan_requests_total`` says the watcher is
    double-emitting.
    """
    ingress = uuid4()
    # One incident planned from _default (unknown alert)...
    unknown = await _seed_incident(db, ingress)
    await _post(
        client,
        _envelope(_payload(unknown, service_name="who", alert_name="Knows"), ingress),
    )
    # ...and one duplicate on it.
    await _post(
        client,
        _envelope(_payload(unknown, service_name="who", alert_name="Knows"), uuid4()),
    )

    scrape = (await client.get("/metrics")).text

    assert 'radar_plans_created_total{outcome="default"} 1.0' in scrape
    assert "radar_duplicate_plan_requests_total 1.0" in scrape


# --- the incident that is not there -----------------------------------------------


async def test_an_unknown_incident_is_422_and_never_mistaken_for_a_duplicate(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A missing incident must not be absorbed by the duplicate handler.

    The plan's foreign key would fail at commit with an IntegrityError — the same
    exception the race raises. If it were not checked first, the handler would answer
    200, mark the event processed, and lose it forever while claiming success. So it
    is checked explicitly: 422, dead-lettered, no marker, visible to a human.
    """
    ingress = uuid4()
    body = _envelope(_payload(uuid4()), ingress)  # incident never seeded

    response = await _post(client, body)

    assert response.status_code == 422
    assert await _count(db, ProcessedEvent) == 0, "no marker — the event is not lost"
    assert await _count(db, InvestigationPlan) == 0
    assert await _count(db, OutboxEvent) == 0


async def test_plan_incident_raises_for_a_missing_incident(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(IncidentNotFoundError):
            await plan_incident(
                session,
                templates=TEMPLATES,
                correlation_id=uuid4(),
                payload=_payload(uuid4()),
            )


async def test_the_sequential_duplicate_is_caught_by_the_precheck_not_the_index(
    client: httpx.AsyncClient, db: Database
) -> None:
    """WHICH guard fired, not merely that the outcome was right.

    Mutation testing found this gap: deleting the pre-check entirely changed no
    assertion, because the unique index absorbs the sequential duplicate too and the
    OUTCOME is identical. The pre-check was, as far as the suite could tell, dead
    code — kept only by a docstring.

    It earns its place by making the common duplicate cheap: no failed INSERT, no
    rollback, no second transaction to rewrite the marker. But a fast path that
    nothing asserts is a fast path that silently rots, so this pins which guard
    actually caught it — the two write DIFFERENT audit payloads, and only the
    pre-check knows the existing plan's id.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    await _post(client, _envelope(_payload(incident_id), ingress))
    plan_id = (await _plans(db))[0].id
    await _post(client, _envelope(_payload(incident_id), ingress))

    async with db.session() as session:
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.event_type == AUDIT_DUPLICATE_IGNORED
                    )
                )
            )
            .scalars()
            .one()
        )

    # The pre-check path names the plan that already exists. The index path cannot:
    # its transaction is rolled back, and it only knows that it lost.
    assert audit.payload["existing_plan_id"] == str(plan_id), (
        "the sequential duplicate must be caught by the PRE-CHECK, not by the "
        "unique index — if this fails, the pre-check is not doing its job and the "
        "cost of every duplicate is a failed insert plus a recovery transaction"
    )
    assert "reason" not in audit.payload, "that field belongs to the race path"
