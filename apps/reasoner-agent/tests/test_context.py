"""The context bundle: does it read LIVE incident state, or a stale snapshot?

There is one test in this file that matters, and it is easy to write in a way that
certifies nothing.

THE VACUOUS VERSION (deliberately not written here)

    seed an incident at CRITICAL -> plan it -> build the bundle -> assert critical

That passes whether or not the rule holds. The severity never changed, so a builder
reading a plan-time snapshot and a builder reading the live row return the SAME answer.
It is the same class of nothing-test as asserting 401-before-422 with valid JSON.

THE VERSION THAT PROVES SOMETHING

    seed the incident at HIGH with alert_count=1
    -> plan it            (any plan-time snapshot now captures high / 1)
    -> ESCALATE it to CRITICAL with alert_count=3   (the divergence)
    -> build the bundle
    -> assert the bundle says CRITICAL and 3

Now the plan-time value and the current value DIFFER, so the test can only pass if the
builder re-queried the incidents row. Both fields in one test, because the rule is that
the builder reads the WHOLE mutable-state set from that row — not just the one field
somebody happened to check.

The severity half is killed by a REAL mutation: ``alerts.severity`` is sitting right
there in the same query that fetches ``alert_name``, it still says ``high`` (that is
what the alert fired at), and reading it is a bug someone would plausibly write.

The alert_count half cannot be killed by a real mutation any more, and that is the
point rather than a gap: ``alert_count`` was stripped out of the event payloads, so
there is no stale copy left to read. The test is the regression guard that stops one
being reintroduced, and it is killed by a synthetic mutation that puts the snapshot
back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from radar_contracts import Severity
from radar_database import Alert, Database, Incident, InvestigationPlan
from radar_reasoner_agent.context import (
    ContextBundle,
    ContextNotAvailableError,
    build_context_bundle,
)

SERVICE = "order-service"
ALERT = "OrderProcessingFailureRate"
FINGERPRINT = "f" * 64
T0 = datetime(2026, 7, 14, 10, 0, 0, tzinfo=UTC)

PLAN_STEPS = [
    {"order": 1, "description": "Check recent deployments"},
    {"order": 2, "description": "Review order-service error logs"},
    {"order": 3, "description": "Check order-db connection pool saturation"},
]


async def _seed_incident(
    db: Database,
    *,
    severity: Severity,
    alert_count: int,
    alerts: int = 1,
) -> UUID:
    """An incident as ingestion leaves it, with ``alerts`` alert rows attached."""
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint=FINGERPRINT,
        service_name=SERVICE,
        title=f"{SERVICE} {ALERT}",
        severity=severity.value,
        status="open",
        alert_count=alert_count,
        opened_at=T0,
        updated_at=T0,
    )
    async with db.session() as session:
        session.add(incident)
        for i in range(alerts):
            session.add(
                Alert(
                    id=uuid4(),
                    source="mock",
                    fingerprint=FINGERPRINT,
                    service_name=SERVICE,
                    alert_name=ALERT,
                    # The severity the ALERT fired at. Escalation raises the
                    # INCIDENT's severity and never touches this — which is exactly
                    # what makes it the wrong field to read.
                    severity=severity.value,
                    status="firing",
                    raw_payload={},
                    fired_at=T0 + timedelta(seconds=i),
                    received_at=T0 + timedelta(seconds=i),
                    incident_id=incident.id,
                    correlation_id=uuid4(),
                )
            )
        await session.commit()
    return incident.id


async def _seed_plan(db: Database, incident_id: UUID) -> UUID:
    """The plan, as the planner writes it."""
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident_id,
        correlation_id=uuid4(),
        steps=PLAN_STEPS,
        template_key=f"{SERVICE}:{ALERT}",
        status="pending",
    )
    async with db.session() as session:
        session.add(plan)
        await session.commit()
    return plan.id


async def _escalate(
    db: Database, incident_id: UUID, *, to: Severity, alert_count: int
) -> None:
    """What the watcher does when alerts keep arriving: raise severity, bump the count.

    This happens AFTER the plan was written, which is the entire point: it creates a
    divergence between the plan-time value and the current one.
    """
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        incident.severity = to.value
        incident.alert_count = alert_count
        for i in range(1, alert_count):
            session.add(
                Alert(
                    id=uuid4(),
                    source="mock",
                    fingerprint=FINGERPRINT,
                    service_name=SERVICE,
                    alert_name=ALERT,
                    severity=Severity.HIGH.value,  # the alerts still fired at high
                    status="firing",
                    raw_payload={},
                    fired_at=T0 + timedelta(minutes=i),
                    received_at=T0 + timedelta(minutes=i),
                    incident_id=incident_id,
                    correlation_id=uuid4(),
                )
            )
        await session.commit()


# --- THE TEST --------------------------------------------------------------------


async def test_the_bundle_reads_live_incident_state_not_the_plan_time_snapshot(
    db: Database,
) -> None:
    """high -> critical AND 1 -> 3, with the divergence created BEFORE the build.

    The incident is planned while it is ``high`` with one alert. Two more alerts
    arrive, the watcher escalates it to ``critical``, and only THEN is the bundle
    built. A builder reading anything captured at plan time returns high/1; only one
    that re-queries the incidents row returns critical/3.

    Both fields in one test on purpose: the rule is that the builder reads the whole
    mutable-state set from that row, not just the field somebody happened to check.
    """
    # 1. The incident as it was when the planner looked at it.
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_id = await _seed_plan(db, incident_id)

    # 2. THE DIVERGENCE. Two more alerts land; the watcher escalates. The plan row is
    #    untouched — it is a decision that was already made.
    await _escalate(db, incident_id, to=Severity.CRITICAL, alert_count=3)

    # 3. Only now is the bundle built.
    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    assert bundle.severity is Severity.CRITICAL, (
        "the bundle carried the PLAN-TIME severity (high). The incident is critical "
        "now — an engineer would read an RCA card that understates a live outage."
    )
    assert bundle.alert_count == 3, (
        "the bundle carried the plan-time alert_count (1). The incident has had three "
        "alerts; the model is reasoning about a quieter incident than the real one."
    )

    # The plan row is untouched — it still records the decision that was made, and the
    # bundle took its STEPS from it while taking its STATE from the incident. That
    # split is the whole rule, and both halves are asserted here.
    assert [s.order for s in bundle.investigation_steps] == [1, 2, 3]


async def test_the_alert_row_still_says_high_which_is_why_it_is_the_wrong_source(
    db: Database,
) -> None:
    """Names the trap explicitly: the stale value is RIGHT THERE, in the same query.

    ``_alert_name_for`` selects from ``alerts``, and ``alerts.severity`` is one column
    over. It says ``high`` — what the alert fired at — while the incident says
    ``critical``. Reading it is a bug someone would plausibly write, and this test
    documents that the divergence is real rather than contrived.
    """
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_id = await _seed_plan(db, incident_id)
    await _escalate(db, incident_id, to=Severity.CRITICAL, alert_count=3)

    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
        assert incident is not None
        alert_severities = {
            a.severity
            for a in (await session.execute(_alerts_of(incident_id))).scalars().all()
        }
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    assert incident.severity == Severity.CRITICAL.value
    assert alert_severities == {Severity.HIGH.value}, "every alert still says high"
    assert bundle.severity is Severity.CRITICAL, "the bundle followed the INCIDENT"


def _alerts_of(incident_id: UUID):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    return select(Alert).where(Alert.incident_id == incident_id)


# --- the rest of the shape ---------------------------------------------------------


async def test_the_steps_come_from_the_plan_row(db: Database) -> None:
    """Steps are the plan's OUTPUT — a decision made, not a value that drifts.

    Reading them from the plan is correct and is NOT a source-of-truth violation. The
    alternative — re-matching the template at reasoning time — would be worse: editing
    the ConfigMap would silently change an investigation that is already in flight.
    """
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_id = await _seed_plan(db, incident_id)

    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    assert [s.order for s in bundle.investigation_steps] == [1, 2, 3]
    assert bundle.investigation_steps[0].description == "Check recent deployments"


async def test_the_bundle_has_the_v1_shape(db: Database) -> None:
    """The exact v1 shape from the plan, including the empty Phase 8 slot."""
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_id = await _seed_plan(db, incident_id)

    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    assert set(bundle.model_dump()) == {
        "incident_id",
        "service_name",
        "alert_name",
        "severity",
        "opened_at",
        "alert_count",
        "investigation_steps",
        "retrieved_context",
    }
    assert bundle.retrieved_context == [], "Phase 8's slot, empty and in shape"
    assert bundle.service_name == SERVICE
    assert bundle.alert_name == ALERT, "not on the incidents row — read from alerts"
    assert bundle.opened_at == T0
    # It serializes: this is stored verbatim on the recommendation, so a future reader
    # can see exactly what the model was shown when it said what it said.
    assert ContextBundle.model_validate(bundle.model_dump()) == bundle


# --- refusing to reason about something that is not there ---------------------------


async def test_a_missing_incident_is_refused(db: Database) -> None:
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_id = await _seed_plan(db, incident_id)

    async with db.session() as session:
        with pytest.raises(ContextNotAvailableError, match="incident"):
            await build_context_bundle(session, incident_id=uuid4(), plan_id=plan_id)


async def test_a_missing_plan_is_refused(db: Database) -> None:
    incident_id = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)

    async with db.session() as session:
        with pytest.raises(ContextNotAvailableError, match="plan"):
            await build_context_bundle(
                session, incident_id=incident_id, plan_id=uuid4()
            )


async def test_a_plan_belonging_to_another_incident_is_refused(db: Database) -> None:
    """Reasoning over a mismatched pair would produce an RCA about one incident using
    another's checklist. Refuse rather than invent."""
    first = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    second = await _seed_incident(db, severity=Severity.HIGH, alert_count=1)
    plan_of_second = await _seed_plan(db, second)

    async with db.session() as session:
        with pytest.raises(ContextNotAvailableError, match="belongs to incident"):
            await build_context_bundle(
                session, incident_id=first, plan_id=plan_of_second
            )
