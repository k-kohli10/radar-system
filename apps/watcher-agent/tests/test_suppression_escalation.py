"""Suppression and escalation: the two live rules, and the two bugs they invite.

Both are enforced against a REAL Postgres with time controlled explicitly, because
both are claims about *time* and a test that lets the clock run is a test that passes
for the wrong reason.

THE TWO BUGS BEING GUARDED

**De-escalation.** Code that blindly writes ``escalate_to`` will happily lower a
critical incident back to high because a later, milder alert arrived — and every test
that only ever *raises* severity will pass while it does. So "upward only" is proven by
trying to LOWER it and asserting refusal, not by watching it rise.

**A decorative window.** Counting alerts over the incident's whole lifetime instead of
the rolling window makes ``within_minutes`` dead config: three alerts across three
hours would escalate exactly like three in ten seconds. So the window is asserted at
BOTH edges — third alert just inside escalates, just outside does not — the same
discipline as ingestion's dedup boundary.

Suppression gets the same both-edge treatment: a follow-on at 9m59s is suppressed, at
10m01s it is planned, and exactly 10m00s is suppressed (inclusive — the same convention
as the dedup window, because two adjacent windows with opposite boundary rules is a bug
waiting to be filed).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import AlertNormalizedPayload, NormalizedAlert, Severity
from radar_database import Alert, AuditLog, Database, Incident, OutboxEvent
from radar_watcher_agent.correlation import (
    AUDIT_ESCALATED,
    AUDIT_PLAN_SUPPRESSED,
    PLAN_REQUESTED_EVENT,
)
from radar_watcher_agent.main import create_app
from radar_watcher_agent.rules import CorrelationRules, load_correlation_rules
from sqlalchemy import select

TOKEN = "w" * 64
SERVICE = "order-service"
FINGERPRINT = "f" * 64

#: The shipped policy — the same rules production runs.
#:   suppression:  OrderServiceHighMemory  -> 10 minutes
#:   escalation:   3 alerts within 2 minutes -> critical
RULES: CorrelationRules = load_correlation_rules(
    Path("apps/watcher-agent/config/correlation-rules.yaml")
)
SUPPRESSED_ALERT = "OrderServiceHighMemory"  # has a 10-minute cooldown
PLAIN_ALERT = "OrderProcessingFailureRate"  # has no suppression rule at all

T0 = datetime(2026, 7, 13, 10, 0, 0, tzinfo=UTC)


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


async def _seed_incident(
    db: Database,
    *,
    alert_name: str,
    opened_at: datetime,
    severity: Severity = Severity.HIGH,
    alert_count: int = 1,
) -> UUID:
    """An incident, as ingestion writes it, opened at an explicit time."""
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint=FINGERPRINT,
        service_name=SERVICE,
        title=f"{SERVICE} {alert_name}",
        severity=severity.value,
        status="open",
        alert_count=alert_count,
        opened_at=opened_at,
        updated_at=opened_at,
    )
    async with db.session() as session:
        session.add(incident)
        await session.commit()
    return incident.id


async def _seed_alert(
    db: Database, incident_id: UUID, *, alert_name: str, received_at: datetime
) -> None:
    """An alert row on that incident, arriving at an explicit time.

    Escalation counts THESE — the alerts ingestion attached — inside the rolling
    window, which is why their ``received_at`` has to be controllable.
    """
    async with db.session() as session:
        session.add(
            Alert(
                id=uuid4(),
                source="mock",
                fingerprint=FINGERPRINT,
                service_name=SERVICE,
                alert_name=alert_name,
                severity=Severity.HIGH.value,
                status="firing",
                raw_payload={},
                fired_at=received_at,
                received_at=received_at,
                incident_id=incident_id,
                correlation_id=uuid4(),
            )
        )
        await session.commit()


def _envelope(
    incident_id: UUID,
    *,
    alert_name: str,
    deduplicated: bool,
    received_at: datetime,
    severity: Severity = Severity.HIGH,
) -> dict[str, Any]:
    alert = NormalizedAlert(
        source="mock",
        fingerprint=FINGERPRINT,
        service_name=SERVICE,
        alert_name=alert_name,
        severity=severity,
        raw_payload={},
        fired_at=received_at,
        received_at=received_at,
    )
    payload = AlertNormalizedPayload(
        **alert.model_dump(), incident_id=incident_id, deduplicated=deduplicated
    )
    return {
        "event_id": str(uuid4()),
        "event_type": "alert.normalized",
        "correlation_id": str(uuid4()),
        "payload": payload.model_dump(mode="json"),
    }


async def _post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})


async def _plan_count(db: Database) -> int:
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
    return len(rows)


async def _severity(db: Database, incident_id: UUID) -> str:
    async with db.session() as session:
        value = await session.scalar(
            select(Incident.severity).where(Incident.id == incident_id)
        )
    return str(value)


async def _audit_types(db: Database) -> list[str]:
    async with db.session() as session:
        rows = (await session.execute(select(AuditLog.event_type))).scalars().all()
    return list(rows)


# ============================================================================
# SUPPRESSION — both edges of the 10-minute cooldown
# ============================================================================


@pytest.mark.parametrize(
    ("gap", "expect_suppressed"),
    [
        pytest.param(timedelta(minutes=9, seconds=59), True, id="9m59s-suppressed"),
        pytest.param(timedelta(minutes=10), True, id="10m00s-suppressed-inclusive"),
        pytest.param(timedelta(minutes=10, seconds=1), False, id="10m01s-planned"),
    ],
)
async def test_suppression_cooldown_boundary(
    client: httpx.AsyncClient, db: Database, gap: timedelta, expect_suppressed: bool
) -> None:
    """A follow-on incident within the cooldown gets no plan; outside it, it does.

    The cooldown is measured from the PREVIOUS incident's opened_at. A boundary, not a
    rounding: inclusive at exactly 10m00s, matching ingestion's dedup window — two
    adjacent windows with opposite boundary conventions would be a bug waiting to
    happen.
    """
    # The incident that already ran an investigation, at T0.
    previous = await _seed_incident(db, alert_name=SUPPRESSED_ALERT, opened_at=T0)
    await _seed_alert(db, previous, alert_name=SUPPRESSED_ALERT, received_at=T0)

    # A NEW incident for the same alert, `gap` later (ingestion's 5-minute dedup window
    # has expired, so it genuinely opened a second incident).
    follow_on = await _seed_incident(
        db, alert_name=SUPPRESSED_ALERT, opened_at=T0 + gap
    )

    response = await _post(
        client,
        _envelope(
            follow_on,
            alert_name=SUPPRESSED_ALERT,
            deduplicated=False,
            received_at=T0 + gap,
        ),
    )

    assert response.status_code == 200
    if expect_suppressed:
        assert await _plan_count(db) == 0, "a flapping alert must not re-investigate"
        assert AUDIT_PLAN_SUPPRESSED in await _audit_types(db), (
            "the decision to do nothing must still be auditable — otherwise a "
            "suppressed incident is indistinguishable from a broken one"
        )
    else:
        assert await _plan_count(db) == 1, "outside the cooldown, investigate again"
        assert AUDIT_PLAN_SUPPRESSED not in await _audit_types(db)


async def test_an_alert_with_no_suppression_rule_is_never_suppressed(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Omitting the entry IS the off-switch (not a zero cooldown, which is refused)."""
    previous = await _seed_incident(db, alert_name=PLAIN_ALERT, opened_at=T0)
    await _seed_alert(db, previous, alert_name=PLAIN_ALERT, received_at=T0)
    follow_on = await _seed_incident(
        db, alert_name=PLAIN_ALERT, opened_at=T0 + timedelta(seconds=30)
    )

    await _post(
        client,
        _envelope(
            follow_on,
            alert_name=PLAIN_ALERT,
            deduplicated=False,
            received_at=T0 + timedelta(seconds=30),
        ),
    )

    assert RULES.suppression_for(PLAIN_ALERT) is None
    assert await _plan_count(db) == 1, "no rule, no suppression — even 30s later"


async def test_suppression_is_scoped_to_the_alert_not_just_the_service(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A different alert on the same service does not suppress this one.

    Scoping to the service alone would silence unrelated failures on a busy service —
    the memory alert would suppress the checkout failure, and nobody would investigate
    the outage.
    """
    other = await _seed_incident(db, alert_name=PLAIN_ALERT, opened_at=T0)
    await _seed_alert(db, other, alert_name=PLAIN_ALERT, received_at=T0)
    follow_on = await _seed_incident(
        db, alert_name=SUPPRESSED_ALERT, opened_at=T0 + timedelta(minutes=1)
    )

    await _post(
        client,
        _envelope(
            follow_on,
            alert_name=SUPPRESSED_ALERT,
            deduplicated=False,
            received_at=T0 + timedelta(minutes=1),
        ),
    )

    assert await _plan_count(db) == 1, "a different alert must not suppress this one"


# ============================================================================
# ESCALATION — the rolling window, both edges
# ============================================================================


@pytest.mark.parametrize(
    ("third_at", "expect_escalated"),
    [
        pytest.param(timedelta(minutes=1, seconds=59), True, id="inside-window"),
        pytest.param(timedelta(minutes=2), True, id="exactly-2m-inclusive"),
        pytest.param(timedelta(minutes=2, seconds=1), False, id="outside-window"),
    ],
)
async def test_escalation_window_is_time_bounded(
    client: httpx.AsyncClient,
    db: Database,
    third_at: timedelta,
    expect_escalated: bool,
) -> None:
    """3 alerts within 2 minutes escalates. The SAME 3 alerts, spread wider, does not.

    This is the test that keeps ``within_minutes`` from being decorative. A count over
    the incident's whole lifetime — or over ``incident.alert_count``, the untimed
    running total — would escalate both cases identically, and the window would be dead
    config nobody noticed.

    The window ends at the arriving alert's own ``received_at`` and reaches back
    ``within_minutes``. The first two alerts sit at T0; only the third moves.
    """
    incident = await _seed_incident(
        db, alert_name=PLAIN_ALERT, opened_at=T0, severity=Severity.HIGH, alert_count=3
    )
    # Two alerts already landed, at T0. The third is the one arriving now.
    await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)
    await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)
    await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0 + third_at)

    response = await _post(
        client,
        _envelope(
            incident,
            alert_name=PLAIN_ALERT,
            deduplicated=True,  # a duplicate: this is the branch escalation lives on
            received_at=T0 + third_at,
        ),
    )

    assert response.status_code == 200
    severity = await _severity(db, incident)
    if expect_escalated:
        assert severity == Severity.CRITICAL.value, "3 in 2 minutes is a burst"
        assert AUDIT_ESCALATED in await _audit_types(db)
    else:
        assert severity == Severity.HIGH.value, (
            "the same 3 alerts spread past the window are not a burst — if this "
            "escalates, the count is not time-bounded and within_minutes is dead"
        )
        assert AUDIT_ESCALATED not in await _audit_types(db)
    # Either way, a duplicate never re-plans.
    assert await _plan_count(db) == 0


async def test_below_the_threshold_does_not_escalate(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Two alerts in the window is not three."""
    incident = await _seed_incident(
        db, alert_name=PLAIN_ALERT, opened_at=T0, alert_count=2
    )
    await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)
    await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)

    await _post(
        client,
        _envelope(incident, alert_name=PLAIN_ALERT, deduplicated=True, received_at=T0),
    )

    assert await _severity(db, incident) == Severity.HIGH.value


# --- the de-escalation guard: prove it by trying to LOWER it -------------------


async def test_escalation_never_lowers_an_incidents_severity(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A critical incident stays critical when a burst would 'escalate' it to critical.

    Same rank, so nothing to do — but the important half is the assertion that the
    watcher did not WRITE anything: no severity change, and no escalation audit row
    claiming it escalated an incident that was already there.
    """
    incident = await _seed_incident(
        db,
        alert_name=PLAIN_ALERT,
        opened_at=T0,
        severity=Severity.CRITICAL,
        alert_count=3,
    )
    for _ in range(3):
        await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)

    await _post(
        client,
        _envelope(
            incident,
            alert_name=PLAIN_ALERT,
            deduplicated=True,
            received_at=T0,
            severity=Severity.CRITICAL,
        ),
    )

    assert await _severity(db, incident) == Severity.CRITICAL.value
    assert AUDIT_ESCALATED not in await _audit_types(db), (
        "an incident already at the target severity was not escalated — recording "
        "that it was would be a lie in the audit trail"
    )


async def test_a_milder_rule_cannot_de_escalate_a_critical_incident(
    client: httpx.AsyncClient,
    db: Database,
    tmp_path: Path,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE de-escalation test: a rule whose escalate_to is MILDER must not apply.

    The bug being guarded is code that blindly writes ``escalate_to`` when the burst
    condition is met. With a `escalate_to: high` rule and an incident already at
    CRITICAL, that code silently downgrades a critical incident because three more
    alerts arrived — the incident gets quieter precisely as it gets worse, which is the
    worst possible direction for the bug to point.

    Needs a config the shipped one cannot express (its only rule escalates to
    critical), so the watcher is booted against a rules file written here.
    """
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "correlation:\n"
        "  default_window_minutes: 5\n"
        "  escalation:\n"
        "    - alert_count_threshold: 3\n"
        "      within_minutes: 2\n"
        "      escalate_to: high\n"  # MILDER than the incident's critical
        "  fingerprint_fields: [service_name, alert_name, severity]\n"
    )
    (tmp_path / "postgres_dsn").write_text(database_url)
    (tmp_path / "agent_token").write_text(TOKEN)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_CORRELATION_RULES_PATH", str(rules_path))

    incident = await _seed_incident(
        db,
        alert_name=PLAIN_ALERT,
        opened_at=T0,
        severity=Severity.CRITICAL,
        alert_count=3,
    )
    for _ in range(3):
        await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)

    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://watcher"
        ) as http:
            response = await _post(
                http,
                _envelope(
                    incident,
                    alert_name=PLAIN_ALERT,
                    deduplicated=True,
                    received_at=T0,
                ),
            )

    assert response.status_code == 200
    # The burst condition WAS met — three alerts inside two minutes. The rule still
    # must not fire, because severity only ever ratchets up.
    assert await _severity(db, incident) == Severity.CRITICAL.value, (
        "a critical incident was de-escalated to high by a burst of alerts"
    )
    assert AUDIT_ESCALATED not in await _audit_types(db)


async def test_escalation_raises_a_low_incident_all_the_way_up(
    client: httpx.AsyncClient, db: Database
) -> None:
    """And the positive case still works: low -> critical in one step."""
    incident = await _seed_incident(
        db, alert_name=PLAIN_ALERT, opened_at=T0, severity=Severity.LOW, alert_count=3
    )
    for _ in range(3):
        await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)

    await _post(
        client,
        _envelope(
            incident,
            alert_name=PLAIN_ALERT,
            deduplicated=True,
            received_at=T0,
            severity=Severity.LOW,
        ),
    )

    assert await _severity(db, incident) == Severity.CRITICAL.value
    async with db.session() as session:
        audit = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == AUDIT_ESCALATED)
                )
            )
            .scalars()
            .one()
        )
    assert audit.payload["escalated_from"] == Severity.LOW.value
    assert audit.payload["escalated_to"] == Severity.CRITICAL.value


async def test_the_escalated_severity_is_the_one_the_plan_carries(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Escalation runs BEFORE the plan is requested, so the planner sees the truth.

    A new incident whose alerts already justify escalation must request its plan at the
    ESCALATED severity — not at the severity it was opened with. Ordering the other way
    would hand the planner and the engineer a stale severity for the incident they are
    about to investigate.
    """
    incident = await _seed_incident(
        db, alert_name=PLAIN_ALERT, opened_at=T0, severity=Severity.LOW, alert_count=3
    )
    for _ in range(3):
        await _seed_alert(db, incident, alert_name=PLAIN_ALERT, received_at=T0)

    await _post(
        client,
        _envelope(
            incident,
            alert_name=PLAIN_ALERT,
            deduplicated=False,  # new incident -> a plan IS requested
            received_at=T0,
            severity=Severity.LOW,
        ),
    )

    async with db.session() as session:
        event = (
            (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == PLAN_REQUESTED_EVENT
                    )
                )
            )
            .scalars()
            .one()
        )
    assert event.payload["severity"] == Severity.CRITICAL.value, (
        "the plan carried the pre-escalation severity — the planner would investigate "
        "a 'low' incident that the watcher had just declared critical"
    )
