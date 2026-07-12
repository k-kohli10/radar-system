"""Deduplication window boundary tests (real Postgres).

The dedup window is a boundary, not a rounding: with an incident opened at ``T0``
and a 5-minute window, an alert whose reference time is ``T0 + 4m59s`` attaches
to it, ``T0 + 5m00s`` still attaches (the comparison is inclusive:
``opened_at >= as_of - window``), and ``T0 + 5m01s`` opens a new incident. These
exercise :func:`persist_alert` against a real Postgres with ``opened_at`` and
``as_of`` controlled explicitly — not the weaker "same fingerprint dedups".

Attaching must not emit a second outbox event (the pipeline already started for
this incident on the first alert); opening a new incident must.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from radar_contracts import NormalizedAlert
from radar_database import Alert, Database, Incident, OutboxEvent
from radar_ingestion.normalizer import AlertSource, compute_fingerprint, normalize
from radar_ingestion.publisher import persist_alert
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

    async with db.session() as session:
        result = await persist_alert(session, _alert(), as_of=T0 + delta)
        await session.commit()

    incidents, events = await _counts(db)

    assert result.deduplicated is expect_dedup
    if expect_dedup:
        # Attached to the existing incident: no new incident, no new outbox
        # event, and the incident's alert_count bumped to 2.
        assert incidents == 1
        assert events == 0
        async with db.session() as session:
            attached = await session.scalar(
                select(Incident.alert_count).where(Incident.id == result.incident_id)
            )
        assert attached == 2
    else:
        # Outside the window: a second incident is opened with its own outbox
        # event, and the seed incident is untouched (still alert_count 1).
        assert incidents == 2
        assert events == 1


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
