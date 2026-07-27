"""The incident-resolution gate (real Postgres).

Commit 4 wires the alert flip (commit 3) to the incident transition (commit 2):
after a resolve clears an incident's firing alerts, the incident itself moves to
``resolved`` — but ONLY when no firing alert remains (ADR 0016: "partial resolution
does not change incident status"). A resolved incident is a CONSEQUENCE of its last
alert clearing.

What gets which treatment (matching the risk):

- **The live path and the closure orderings are real and reachable**, so they run
  against real Postgres: a matched resolve resolves the incident; the two
  orderings of a firing and a resolving webhook both converge on a safe state
  (never a resolved incident that still holds a firing alert); and concurrent
  duplicate resolves converge on ONE resolved incident with ONE audit row.
- **The gate's "hold back while a firing alert remains" branch is NOT reachable by
  today's pipeline** — every alert on an incident shares one fingerprint, so one
  resolve clears them all and zero ever remain. It is proven synthetically, against
  a hand-built state the pipeline cannot produce, and the test SAYS SO. That test
  proves the gate's LOGIC, not a live path; it is deliberately weaker evidence than
  the reachable tests, and labelled as such.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from radar_contracts import NormalizedAlert
from radar_database import Alert, AuditLog, Database, Incident
from radar_ingestion.normalizer import AlertSource, compute_fingerprint, normalize
from radar_ingestion.publisher import persist_alert
from radar_ingestion.resolver import resolve_incident_if_quiet
from sqlalchemy import func, select

SERVICE = "order-service"
ALERT_NAME = "OrderFailure"
SEVERITY = "critical"
FINGERPRINT = compute_fingerprint(SERVICE, ALERT_NAME, SEVERITY)
T0 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)


def _firing() -> NormalizedAlert:
    return normalize(
        AlertSource.PROMETHEUS,
        {
            "status": "firing",
            "labels": {
                "service": SERVICE,
                "alertname": ALERT_NAME,
                "severity": SEVERITY,
            },
            "startsAt": T0.isoformat(),
        },
        correlation_id=uuid4(),
    )


def _resolve(*, ended_at: datetime) -> NormalizedAlert:
    return normalize(
        AlertSource.PROMETHEUS,
        {
            "status": "resolved",
            "labels": {
                "service": SERVICE,
                "alertname": ALERT_NAME,
                "severity": SEVERITY,
            },
            "startsAt": T0.isoformat(),
            "endsAt": ended_at.isoformat(),
        },
        correlation_id=uuid4(),
    )


async def _persist(db: Database, alert: NormalizedAlert) -> None:
    async with db.session() as session:
        await persist_alert(session, alert, as_of=alert.received_at)
        await session.commit()


async def _incident_by_fingerprint(db: Database) -> Incident:
    async with db.session() as session:
        incident = (
            await session.execute(
                select(Incident).where(Incident.fingerprint == FINGERPRINT)
            )
        ).scalar_one()
    return incident


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _no_resolved_incident_holds_a_firing_alert(db: Database) -> bool:
    """The invariant every ordering must preserve."""
    async with db.session() as session:
        offenders = (
            await session.execute(
                select(func.count())
                .select_from(Incident)
                .join(Alert, Alert.incident_id == Incident.id)
                .where(Incident.status == "resolved", Alert.status == "firing")
            )
        ).scalar()
    return int(offenders or 0) == 0


# --- the live path ---------------------------------------------------------------


async def test_matched_resolve_transitions_incident_to_resolved(db: Database) -> None:
    """The reachable end-to-end path: fire, then resolve, and the incident resolves.

    One fingerprint, so the single resolve clears the only firing alert and the gate
    finds none remaining — the incident moves open -> resolved, stamped with the
    webhook time, with an ``incident.resolved`` audit row.
    """
    await _persist(db, _firing())
    ended = T0 + timedelta(minutes=20)
    await _persist(db, _resolve(ended_at=ended))

    incident = await _incident_by_fingerprint(db)
    assert incident.status == "resolved"
    assert incident.resolved_at == ended

    async with db.session() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "incident.resolved")
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].payload == {"resolved_by": "alert_resolution"}
    assert audits[0].actor == "ingestion"
    assert await _no_resolved_incident_holds_a_firing_alert(db)


# --- the two orderings both converge safely --------------------------------------


async def test_order_fire_then_resolve_converges_resolved(db: Database) -> None:
    """fire -> resolve: incident opens, then resolves. No firing alert left behind."""
    await _persist(db, _firing())
    await _persist(db, _resolve(ended_at=T0 + timedelta(minutes=5)))

    assert await _count(db, Incident) == 1
    incident = await _incident_by_fingerprint(db)
    assert incident.status == "resolved"
    assert await _no_resolved_incident_holds_a_firing_alert(db)


async def test_order_resolve_then_fire_converges_open(db: Database) -> None:
    """resolve -> fire: the resolve finds no live incident and is ignored; the fire
    then opens a fresh OPEN incident with a firing alert.

    The other convergence point: because the resolve created nothing, the later fire
    is a normal new incident. There is no resolved incident at all, so trivially none
    holds a firing alert. Proves the two orderings don't cross-contaminate — a
    resolve never pre-resolves an incident that hasn't opened.
    """
    await _persist(db, _resolve(ended_at=T0))
    await _persist(db, _firing())

    assert await _count(db, Incident) == 1
    incident = await _incident_by_fingerprint(db)
    assert incident.status == "open"
    alerts = await _alerts_for_incident(db, incident.id)
    assert [a.status for a in alerts] == ["firing"]
    assert await _no_resolved_incident_holds_a_firing_alert(db)


async def _alerts_for_incident(db: Database, incident_id: UUID) -> list[Alert]:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(Alert).where(Alert.incident_id == incident_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# --- concurrent duplicate resolves converge on one resolution --------------------


async def test_concurrent_duplicate_resolves_resolve_once(db: Database) -> None:
    """Two resolve webhooks race the same live incident; exactly one resolves it.

    Reachable: Alertmanager retries can deliver two resolves at once. The lock in
    ``find_resolvable_incident`` serialises them — the winner resolves the incident,
    and the loser's locked lookup re-evaluates the ``{open, investigating}``
    predicate after the winner commits, finds the incident now ``resolved``
    (terminal), and returns None, so the loser records an ignore. One resolved
    incident, one ``incident.resolved`` audit row, no error raised, and no resolved
    incident holding a firing alert.
    """
    await _persist(db, _firing())
    barrier = asyncio.Barrier(2)

    async def resolve() -> None:
        async with db.session() as session:
            await barrier.wait()
            await persist_alert(
                session, _resolve(ended_at=T0 + timedelta(minutes=5)), as_of=T0
            )
            await session.commit()

    # return_exceptions surfaces any raise as a value so the assert names it.
    outcomes = await asyncio.gather(resolve(), resolve(), return_exceptions=True)
    assert all(not isinstance(o, BaseException) for o in outcomes), outcomes

    incident = await _incident_by_fingerprint(db)
    assert incident.status == "resolved"
    async with db.session() as session:
        resolved_audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "incident.resolved")
        )
    assert resolved_audits == 1
    assert await _no_resolved_incident_holds_a_firing_alert(db)


# --- the gate's logic, proven synthetically (pipeline-unreachable state) ----------


async def test_gate_holds_incident_while_a_firing_alert_remains(db: Database) -> None:
    """The gate's hold-back branch, against a state the pipeline CANNOT produce.

    A live incident carrying one firing AND one resolved alert is hand-built here.
    The real pipeline never reaches it: every alert on an incident shares one
    fingerprint, so a single resolve flips them all at once and none is ever left
    firing beside a resolved sibling. This test therefore proves the gate's LOGIC —
    "resolve only when zero firing remain" — and NOT any reachable path. It is weaker
    evidence than the orderings above, and says so on purpose.

    The gate must hold the incident open while the one firing alert remains, then
    resolve it once that alert clears. MUTATION: remove the firing-count guard in
    ``resolve_incident_if_quiet`` (always transition) → the first call resolves the
    incident despite a firing alert, and the ``is False`` / stays-``investigating``
    assertions go red — the exact "resolved incident holding a firing alert" the gate
    exists to prevent.
    """
    incident_id = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint=FINGERPRINT,
                service_name=SERVICE,
                title="synthetic",
                severity=SEVERITY,
                status="investigating",
                alert_count=2,
                opened_at=T0,
            )
        )
        # One firing, one already resolved — the mixed state the pipeline can't make.
        session.add(
            Alert(
                id=uuid4(),
                source="prometheus",
                fingerprint=FINGERPRINT,
                service_name=SERVICE,
                alert_name=ALERT_NAME,
                severity=SEVERITY,
                status="resolved",
                raw_payload={},
                fired_at=T0,
                resolved_at=T0 + timedelta(minutes=1),
                incident_id=incident_id,
                correlation_id=uuid4(),
            )
        )
        the_firing_alert = uuid4()
        session.add(
            Alert(
                id=the_firing_alert,
                source="prometheus",
                fingerprint=FINGERPRINT,
                service_name=SERVICE,
                alert_name=ALERT_NAME,
                severity=SEVERITY,
                status="firing",
                raw_payload={},
                fired_at=T0,
                incident_id=incident_id,
                correlation_id=uuid4(),
            )
        )
        await session.commit()

    # A firing alert remains: the gate must NOT resolve the incident.
    async with db.session() as session:
        did = await resolve_incident_if_quiet(
            session, incident_id=incident_id, resolved_at=T0 + timedelta(minutes=10)
        )
        await session.commit()
    assert did is False
    assert (await _one(db, incident_id)).status == "investigating"

    # Clear the last firing alert; now the gate resolves the incident.
    async with db.session() as session:
        alert = await session.get(Alert, the_firing_alert)
        assert alert is not None
        alert.status = "resolved"
        alert.resolved_at = T0 + timedelta(minutes=10)
        await session.commit()

    async with db.session() as session:
        did = await resolve_incident_if_quiet(
            session, incident_id=incident_id, resolved_at=T0 + timedelta(minutes=10)
        )
        await session.commit()
    assert did is True
    assert (await _one(db, incident_id)).status == "resolved"


async def _one(db: Database, incident_id: UUID) -> Incident:
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    return incident
