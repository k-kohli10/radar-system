"""Deduplication window boundary tests (real Postgres).

The dedup window is a boundary, not a rounding: with an incident opened at ``T0``
and a 5-minute window, an alert whose reference time is ``T0 + 4m59s`` attaches
to it, ``T0 + 5m00s`` still attaches (the comparison is inclusive:
``opened_at >= as_of - window``), and ``T0 + 5m01s`` opens a new incident. These
exercise :func:`persist_alert` against a real Postgres with ``opened_at`` and
``as_of`` controlled explicitly — not the weaker "same fingerprint dedups".

**Both branches publish an ``alert.normalized`` event.** Attaching emits one just
as opening does — the watcher enforces suppression and escalation, and escalation
is a claim about arrival rate ("3 alerts within 2 minutes"), so it cannot be
enforced against alerts the watcher never sees. What distinguishes the branches is
the payload: the event carries ``incident_id`` (which incident the alert landed
on) and ``deduplicated`` (whether that incident already existed), so these tests
pin the payload, not merely the event count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from radar_contracts import NormalizedAlert
from radar_database import Alert, AuditLog, Database, Incident, OutboxEvent
from radar_ingestion.normalizer import AlertSource, compute_fingerprint, normalize
from radar_ingestion.publisher import (
    ALERT_ATTACHED_AUDIT_EVENT,
    ALERT_NORMALIZED_EVENT,
    WATCHER_TARGET,
    persist_alert,
)
from sqlalchemy import func, select

SERVICE = "order-service"
ALERT_NAME = "OrderFailure"
SEVERITY = "critical"
T0 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)


def _alert() -> NormalizedAlert:
    """A mock-source normalized alert whose fingerprint matches the seed."""
    return normalize(
        AlertSource.MOCK,
        {
            "service_name": SERVICE,
            "alert_name": ALERT_NAME,
            "severity": SEVERITY,
            "fired_at": T0.isoformat(),
        },
    )


async def _seed_open_incident(db: Database) -> None:
    """Insert one open incident opened exactly at ``T0`` (no outbox event)."""
    async with db.session() as session:
        session.add(
            Incident(
                correlation_id=uuid4(),
                fingerprint=compute_fingerprint(SERVICE, ALERT_NAME, SEVERITY),
                service_name=SERVICE,
                title="Orders failing",
                severity=SEVERITY,
                status="open",
                alert_count=1,
                opened_at=T0,
                updated_at=T0,
            )
        )
        await session.commit()


async def _counts(db: Database) -> tuple[int, int]:
    """Return ``(incident_count, outbox_event_count)`` across the DB."""
    async with db.session() as session:
        incidents = await session.scalar(select(func.count()).select_from(Incident))
        events = await session.scalar(select(func.count()).select_from(OutboxEvent))
    return incidents or 0, events or 0


async def _sole_outbox_event(db: Database) -> OutboxEvent:
    """Return the one outbox event in the DB, asserting there is exactly one."""
    async with db.session() as session:
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize(
    ("delta", "expect_dedup"),
    [
        pytest.param(timedelta(minutes=4, seconds=59), True, id="4m59s-attaches"),
        pytest.param(timedelta(minutes=5), True, id="5m00s-attaches-inclusive"),
        pytest.param(timedelta(minutes=5, seconds=1), False, id="5m01s-opens-new"),
    ],
)
async def test_dedup_window_boundary(
    db: Database, delta: timedelta, expect_dedup: bool
) -> None:
    await _seed_open_incident(db)
    alert = _alert()

    async with db.session() as session:
        result = await persist_alert(session, alert, as_of=T0 + delta)
        await session.commit()

    incidents, events = await _counts(db)

    assert result.deduplicated is expect_dedup

    # Either side of the boundary, the watcher is told: exactly one event, and it
    # names the incident the alert landed on and whether that incident is new.
    assert events == 1
    event = await _sole_outbox_event(db)
    assert event.event_type == ALERT_NORMALIZED_EVENT
    assert event.target_service == WATCHER_TARGET
    assert event.correlation_id == alert.correlation_id
    assert event.payload["incident_id"] == str(result.incident_id)
    assert event.payload["deduplicated"] is expect_dedup

    if expect_dedup:
        # Attached to the seeded incident: no new incident, alert_count bumped.
        assert incidents == 1
        async with db.session() as session:
            attached = await session.scalar(
                select(Incident.alert_count).where(Incident.id == result.incident_id)
            )
        assert attached == 2
    else:
        # Outside the window: a second incident is opened, and the event names
        # that new one — not the seeded incident it declined to attach to.
        assert incidents == 2
        async with db.session() as session:
            seed_count = await session.scalar(
                select(Incident.alert_count).where(Incident.id != result.incident_id)
            )
        assert seed_count == 1


async def test_attached_alert_links_to_the_existing_incident(db: Database) -> None:
    """A deduplicated alert row is written and linked to the matched incident."""
    await _seed_open_incident(db)
    alert = _alert()

    async with db.session() as session:
        result = await persist_alert(session, alert, as_of=T0 + timedelta(minutes=1))
        await session.commit()

    assert result.deduplicated is True
    async with db.session() as session:
        incident_id = await session.scalar(
            select(Alert.incident_id).where(Alert.id == alert.id)
        )
        alert_rows = await session.scalar(select(func.count()).select_from(Alert))
    assert incident_id == result.incident_id
    assert alert_rows == 1

    # The attach is audited: one alert_attached row for the incident, carrying the
    # bumped alert_count escalation reads off. Drop the audit write in persist_alert
    # and this goes red — the attach then leaves no trail.
    async with db.session() as session:
        attach = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.event_type == ALERT_ATTACHED_AUDIT_EVENT
                )
            )
        ).scalar_one()
    assert attach.entity_type == "incident"
    assert attach.entity_id == result.incident_id
    assert attach.actor == "ingestion"
    assert attach.payload["alert_count"] == 2
