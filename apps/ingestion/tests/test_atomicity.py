"""Ingestion write-path atomicity (real Postgres).

Opening a new incident issues three rows in one transaction — the incident, its
alert, and the ``alert.normalized`` outbox event — and :func:`persist_alert`
deliberately does not commit: the route owns the boundary so the trio is
all-or-nothing. These pin both directions: on commit all three land (linked and
consistent), and on a failure induced mid-transaction — after the rows are
flushed to the database but before commit — the whole trio rolls back, leaving
no orphan incident, alert, or outbox event.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from radar_contracts import NormalizedAlert
from radar_database import Alert, Database, Incident, OutboxEvent
from radar_ingestion.normalizer import AlertSource, normalize
from radar_ingestion.publisher import ALERT_NORMALIZED_EVENT, persist_alert
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


class _InducedError(Exception):
    """Stand-in for any error thrown in the route between persist and commit."""


def _alert(correlation_id: UUID | None = None) -> NormalizedAlert:
    """A normalized alert, optionally threaded with the ingress correlation id."""
    return normalize(
        AlertSource.MOCK,
        {
            "service_name": "order-service",
            "alert_name": "OrderFailure",
            "severity": "critical",
            "fired_at": "2026-07-09T10:00:00Z",
        },
        correlation_id=correlation_id,
    )


async def _counts(db: Database) -> tuple[int, int, int]:
    """Return ``(incidents, alerts, outbox_events)`` across the DB."""
    async with db.session() as session:
        incidents = await session.scalar(select(func.count()).select_from(Incident))
        alerts = await session.scalar(select(func.count()).select_from(Alert))
        events = await session.scalar(select(func.count()).select_from(OutboxEvent))
    return incidents or 0, alerts or 0, events or 0


async def test_new_incident_trio_commits_together(db: Database) -> None:
    # The correlation id minted at ingress and threaded onto the alert; the
    # incident, alert, and outbox event must all carry it (Phase 10 traces the
    # whole pipeline by correlation_id, so this equality is load-bearing).
    ingress_id = uuid4()
    alert = _alert(ingress_id)

    async with db.session() as session:
        result = await persist_alert(session, alert)
        await session.commit()

    incidents, alerts, events = await _counts(db)
    assert (incidents, alerts, events) == (1, 1, 1)
    assert result.deduplicated is False

    async with db.session() as session:
        alert_incident_id, alert_corr = (
            await session.execute(
                select(Alert.incident_id, Alert.correlation_id).where(
                    Alert.id == alert.id
                )
            )
        ).one()
        incident_corr = await session.scalar(
            select(Incident.correlation_id).where(Incident.id == result.incident_id)
        )
        event_type, event_corr = (
            await session.execute(
                select(OutboxEvent.event_type, OutboxEvent.correlation_id).where(
                    OutboxEvent.event_type == ALERT_NORMALIZED_EVENT
                )
            )
        ).one()

    # The alert links to the incident just opened and the event is the
    # alert.normalized one that drives the watcher.
    assert alert_incident_id == result.incident_id
    assert event_type == ALERT_NORMALIZED_EVENT
    # All three rows carry the same correlation_id, equal to the ingress id —
    # not just FK linkage. This is what trace-by-correlation_id depends on.
    assert incident_corr == alert_corr == event_corr == ingress_id


async def test_new_incident_trio_rolls_back_together_on_failure(db: Database) -> None:
    alert = _alert()

    with pytest.raises(_InducedError):
        async with db.session() as session:
            await persist_alert(session, alert)
            # Flush so the incident, alert, and outbox event genuinely reach the
            # database inside this transaction — then fail before commit. The
            # rollback must take all three with it, not just the uncommitted tail.
            await session.flush()
            raise _InducedError

    # Nothing survives: no orphan incident, alert, or outbox event.
    assert await _counts(db) == (0, 0, 0)


async def test_induced_integrity_error_rolls_back_the_whole_trio(db: Database) -> None:
    """A real DB-side failure (a duplicate incident) rolls the trio back too."""
    alert = _alert()

    with pytest.raises(IntegrityError):
        async with db.session() as session:
            await persist_alert(session, alert)
            # persist_alert opened an incident with correlation_id ==
            # alert.correlation_id (unique). A second incident reusing it forces
            # a unique-constraint violation at commit, mid-transaction.
            session.add(
                Incident(
                    id=uuid4(),
                    correlation_id=alert.correlation_id,
                    fingerprint=alert.fingerprint,
                    service_name=alert.service_name,
                    title="dup",
                    severity=alert.severity.value,
                    status="open",
                )
            )
            await session.commit()

    assert await _counts(db) == (0, 0, 0)
