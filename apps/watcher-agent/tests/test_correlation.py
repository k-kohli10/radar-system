"""Incident resolution, the plan_requested trigger, and the properties they must hold.

Four things are pinned here, each of which is invisible unless the test is written to
see it:

**Correlation-id threading.** Every test anchors on an INGRESS uuid — the value
ingestion minted — and asserts *equality back to it* across the incident row, the
audit record, and the plan_requested event. Not "an id is present", not "the rows agree
with each other": equal to the original. A watcher that minted a fresh UUID anywhere
would produce rows that are perfectly self-consistent and still break Phase 10's
"trace the pipeline by correlation_id alone", and only an equality assertion against
the ingress value catches it.

**The transaction boundary.** plan_requested, the audit row, and the processed_events
marker commit together or not at all. Proven by inducing a failure after they are all
flushed to the database and asserting nothing survives.

**The dedup branch must not re-emit.** A duplicate alert emits ZERO plan_requested
events. Emitting a second one would re-plan an incident already downstream — a second
investigation, a second LLM call, and two RCA cards for one incident.

**Redelivery changes nothing.** The gate now has real state to protect: the same event
twice must leave exactly one plan_requested and one audit row.

WHERE THE CHAIN IS ANCHORED. These tests seed the incident the way ingestion writes it
(same fields, same ingress correlation id) rather than importing ingestion, which is a
separate service the watcher must not depend on. So they prove the watcher *threads*
the value it was given. That ingestion actually mints and stores that same value is
proven where the two services really meet — the end-to-end test — and the two together
make the chain whole.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import (
    AlertNormalizedPayload,
    NormalizedAlert,
    PlanRequestedPayload,
    Severity,
)
from radar_database import (
    AuditLog,
    Database,
    Incident,
    OutboxEvent,
    ProcessedEvent,
    mark_processed,
)
from radar_watcher_agent.config import SERVICE_NAME
from radar_watcher_agent.correlation import (
    AUDIT_ALERT_ATTACHED,
    AUDIT_PLAN_REQUESTED,
    PLAN_REQUESTED_EVENT,
    PLANNER_TARGET,
    IncidentNotFoundError,
    correlate,
)
from radar_watcher_agent.main import create_app
from radar_watcher_agent.rules import load_correlation_rules
from sqlalchemy import func, select

TOKEN = "w" * 64
SERVICE = "order-service"
ALERT = "OrderProcessingFailureRate"
#: The watcher never recomputes the fingerprint — it trusts the one ingestion
#: computed (ADR 0013). So this suite needs no dependency on the ingestion package;
#: any 64-char stand-in serves, and using one keeps the services decoupled.
FINGERPRINT = "f" * 64
#: The rules the shipped ConfigMap declares — the same policy production runs.
RULES = load_correlation_rules(Path("apps/watcher-agent/config/correlation-rules.yaml"))


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
            transport=transport, base_url="http://watcher"
        ) as http:
            yield http


def _alert(ingress: UUID, *, severity: Severity = Severity.HIGH) -> NormalizedAlert:
    return NormalizedAlert(
        source="mock",
        fingerprint=FINGERPRINT,
        service_name=SERVICE,
        alert_name=ALERT,
        severity=severity,
        raw_payload={},
        fired_at=datetime.now(UTC),
        correlation_id=ingress,
    )


async def _seed_incident(
    db: Database,
    ingress: UUID,
    *,
    severity: Severity = Severity.HIGH,
    alert_count: int = 1,
) -> UUID:
    """Write the incident exactly as ingestion writes it, carrying the ingress id.

    ``incidents.correlation_id`` is UNIQUE and ingestion sets it to the correlation id
    minted for the alert that opened the incident. That is the anchor of the whole
    chain, so it is what these tests assert back to.
    """
    incident = Incident(
        id=uuid4(),
        correlation_id=ingress,
        fingerprint=FINGERPRINT,
        service_name=SERVICE,
        title=f"{SERVICE} {ALERT}",
        severity=severity.value,
        status="open",
        alert_count=alert_count,
    )
    async with db.session() as session:
        session.add(incident)
        await session.commit()
    return incident.id


def _payload(
    incident_id: UUID, ingress: UUID, *, deduplicated: bool, **over: Any
) -> AlertNormalizedPayload:
    alert = _alert(ingress)
    return AlertNormalizedPayload(
        **(alert.model_dump() | over),
        incident_id=incident_id,
        deduplicated=deduplicated,
    )


def _envelope(payload: AlertNormalizedPayload, ingress: UUID) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": "alert.normalized",
        "correlation_id": str(ingress),
        "payload": payload.model_dump(mode="json"),
    }


async def _plan_events(db: Database) -> list[OutboxEvent]:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == PLAN_REQUESTED_EVENT
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _audits(db: Database) -> list[AuditLog]:
    async with db.session() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return list(rows)


async def _count(db: Database, model: Any) -> int:
    async with db.session() as session:
        n = await session.scalar(select(func.count()).select_from(model))
    return int(n or 0)


# --- 1. CORRELATION-ID THREADING ----------------------------------------------


async def test_correlation_id_threads_from_ingress_to_plan_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The whole chain, anchored to the ONE value ingress minted.

    Every row is asserted equal to INGRESS itself — not merely equal to each other. A
    watcher that minted a new UUID for its outbox event would produce a perfectly
    self-consistent set of rows and still sever the trace; only equality back to the
    original catches that.

    processed_events carries no correlation_id column (it is keyed (event_id,
    processed_by) — see the Phase 3 schema), so the marker is anchored by the event_id
    of the very envelope that carried INGRESS. The audit_log row is the watcher-side
    row that does carry the correlation id, and it is asserted too.
    """
    ingress = uuid4()  # the value ingestion minted at the door
    incident_id = await _seed_incident(db, ingress)
    payload = _payload(incident_id, ingress, deduplicated=False)
    body = _envelope(payload, ingress)

    response = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 200

    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
        marker = await session.get(
            ProcessedEvent, (UUID(body["event_id"]), SERVICE_NAME)
        )
    assert incident is not None
    events = await _plan_events(db)
    audits = await _audits(db)
    assert len(events) == 1
    assert len(audits) == 1

    # THE ASSERTION. Every link equals the ingress value itself.
    assert incident.correlation_id == ingress, "ingestion's incident row"
    assert audits[0].correlation_id == ingress, "the watcher's audit record"
    assert events[0].correlation_id == ingress, "the plan_requested event"

    # And the marker is anchored to the envelope that carried that id.
    assert marker is not None
    assert marker.event_id == UUID(body["event_id"])
    assert marker.processed_by == SERVICE_NAME

    # Stated as one chain, because that is the property: one value, end to end.
    assert (
        ingress
        == incident.correlation_id
        == audits[0].correlation_id
        == events[0].correlation_id
    )


async def test_the_watcher_never_mints_a_correlation_id(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Belt and braces: no row the watcher writes carries any OTHER uuid.

    The previous test would still pass if the watcher wrote a second, extra event with
    a fresh id. This asserts the negative: across everything the watcher produced,
    exactly one correlation id exists, and it is the one it was handed.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    body = _envelope(_payload(incident_id, ingress, deduplicated=False), ingress)

    await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})

    async with db.session() as session:
        event_ids = set(
            (await session.execute(select(OutboxEvent.correlation_id))).scalars().all()
        )
        audit_ids = set(
            (await session.execute(select(AuditLog.correlation_id))).scalars().all()
        )

    assert event_ids == {ingress}
    assert audit_ids == {ingress}


# --- 2. THE TRANSACTION BOUNDARY -----------------------------------------------


async def test_plan_requested_marker_and_audit_commit_together(db: Database) -> None:
    """All three land, in one transaction, or none of them do.

    Driven at the session level (not over HTTP) so the failure can be induced AFTER the
    rows have genuinely reached the database and BEFORE the commit — which is the only
    window where a split boundary is observable.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    payload = _payload(incident_id, ingress, deduplicated=False)
    event_id = uuid4()

    with pytest.raises(_InducedError):
        async with db.session() as session:
            await correlate(
                session, rules=RULES, correlation_id=ingress, payload=payload
            )
            await mark_processed(session, event_id, SERVICE_NAME)
            # Flush so all three rows really exist inside this transaction, then fail.
            await session.flush()
            raise _InducedError

    # Nothing survives. In particular there is no marker: had one committed without its
    # event, the gate would skip the redelivery and this incident would never be
    # planned — a permanent, silent stall.
    assert await _count(db, OutboxEvent) == 0
    assert await _count(db, AuditLog) == 0
    assert await _count(db, ProcessedEvent) == 0
    # The seeded incident is untouched — it was committed before.
    assert await _count(db, Incident) == 1


# --- 3. THE DEDUP BRANCH MUST NOT RE-EMIT ---------------------------------------


async def test_a_new_incident_emits_exactly_one_plan_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    body = _envelope(_payload(incident_id, ingress, deduplicated=False), ingress)

    await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})

    events = await _plan_events(db)
    assert len(events) == 1
    event = events[0]
    assert event.target_service == PLANNER_TARGET
    # The planner matches its template on service_name:alert_name, and alert_name is
    # not on the incidents table — so the watcher must pass it through.
    plan = PlanRequestedPayload.model_validate(event.payload)
    assert plan.incident_id == incident_id
    assert plan.service_name == SERVICE
    assert plan.alert_name == ALERT

    audits = await _audits(db)
    assert [a.event_type for a in audits] == [AUDIT_PLAN_REQUESTED]


async def test_a_deduplicated_alert_emits_no_plan_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    """ZERO. The incident is already being planned.

    A second plan_requested would re-plan an incident already downstream: the planner
    builds another investigation, the reasoner spends another LLM call, and the engineer
    gets two RCA cards for one incident. The dedup branch feeds escalation (next
    commit) — it does not restart the pipeline.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress, alert_count=2)
    body = _envelope(_payload(incident_id, ingress, deduplicated=True), ingress)

    response = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 200
    assert await _plan_events(db) == []
    # It was still SEEN — the audit trail records the attach, and the marker is written,
    # so the worker stops redelivering it.
    audits = await _audits(db)
    assert [a.event_type for a in audits] == [AUDIT_ALERT_ATTACHED]
    assert await _count(db, ProcessedEvent) == 1


async def test_the_dedup_branch_emits_nothing_even_after_a_planned_incident(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The realistic sequence: first alert plans, the next two do not.

    Three alerts on one incident must produce exactly ONE investigation — which is the
    entire point of deduplication reaching the watcher at all.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)

    await client.post(
        "/events",
        json=_envelope(_payload(incident_id, ingress, deduplicated=False), ingress),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )
    for _ in range(2):
        await client.post(
            "/events",
            json=_envelope(_payload(incident_id, ingress, deduplicated=True), ingress),
            headers={AGENT_TOKEN_HEADER: TOKEN},
        )

    assert len(await _plan_events(db)) == 1, "one incident, one investigation"
    assert await _count(db, ProcessedEvent) == 3, "all three alerts were handled"


# --- 4. REPEAT-DISPATCH IDEMPOTENCY (now with real state) ------------------------


async def test_redelivery_creates_no_second_plan_requested(
    client: httpx.AsyncClient, db: Database
) -> None:
    """THE test that makes the processed_events gate real rather than decorative.

    The same envelope, dispatched twice — as the at-least-once worker will do whenever a
    dispatch times out after the handler committed. Exactly one plan_requested, one
    audit row, one marker. A second plan_requested would mean a duplicate investigation
    for every redelivered event, which is precisely the failure the gate exists to
    prevent.
    """
    ingress = uuid4()
    incident_id = await _seed_incident(db, ingress)
    body = _envelope(_payload(incident_id, ingress, deduplicated=False), ingress)

    first = await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})
    second = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert first.json() == {"status": "processed"}
    assert second.json() == {"status": "already_processed"}

    assert len(await _plan_events(db)) == 1, "the duplicate must not re-plan"
    assert len(await _audits(db)) == 1, "and must not re-audit"
    assert await _count(db, ProcessedEvent) == 1


# --- the impossible incident -----------------------------------------------------


async def test_an_unknown_incident_is_rejected_and_leaves_no_marker(
    client: httpx.AsyncClient, db: Database
) -> None:
    """422 (permanent → dead-letter), and crucially NO marker.

    Marking it processed would bury the event: the worker would stop retrying and the
    audit trail would say the watcher handled an incident that does not exist. Failing
    without a marker leaves the event dead-lettered and visible to a human.
    """
    ingress = uuid4()
    body = _envelope(_payload(uuid4(), ingress, deduplicated=False), ingress)

    response = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 422
    assert await _count(db, ProcessedEvent) == 0
    assert await _count(db, OutboxEvent) == 0


async def test_correlate_raises_for_a_missing_incident(db: Database) -> None:
    async with db.session() as session:
        with pytest.raises(IncidentNotFoundError):
            await correlate(
                session,
                rules=RULES,
                correlation_id=uuid4(),
                payload=_payload(uuid4(), uuid4(), deduplicated=False),
            )


async def test_a_payload_missing_incident_id_is_422(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The payload contract is enforced: ingestion constructs it, the watcher
    parses it — a body that is not the agreed shape is a 422, not a guess."""
    ingress = uuid4()
    alert = _alert(ingress)
    body = {
        "event_id": str(uuid4()),
        "event_type": "alert.normalized",
        "correlation_id": str(ingress),
        "payload": alert.model_dump(mode="json"),  # no incident_id, no deduplicated
    }

    response = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 422
    assert await _count(db, ProcessedEvent) == 0
