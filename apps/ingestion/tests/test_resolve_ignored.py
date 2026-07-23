"""A resolved alert matching no open incident opens nothing (real Postgres).

The bug this pins: a ``resolved`` alert used to fall through to the create path.
With no open incident inside the 5-minute dedup window, the resolve **opened a
brand-new incident** in ``open`` and published ``alert.normalized`` — so the
watcher requested an investigation plan for an alert that was already over. And
this is the common case: an incident's alert resolves minutes or hours after it
opened, well outside the dedup window, so almost every resolve took that path.

The guard's guarantee is a three-way "nothing happened" plus one receipt:
:func:`persist_alert` for such an alert writes **zero incidents, zero alerts, and
zero outbox events**, and exactly **one** ``audit_log`` row recording that the
resolve was received and ignored. All three zeros matter together — an incident
without an event would still be a phantom incident; an event without an incident
would still wake the watcher; an alert row would reintroduce the NULL-FK orphan
this design rejected. The load-bearing test asserts all four counts at once, so
deleting the guard (letting the resolve fall through to the create path) turns the
``(0, 0, 0, 1)`` assertion red rather than slipping past a partial check.

Controls: a firing alert with no open incident still opens one (the guard is
scoped to ``resolved``, not "no match"), and a resolve that DOES match an open
incident is deliberately NOT covered here — that path is the next two commits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from radar_contracts import NormalizedAlert
from radar_database import Alert, AuditLog, Database, Incident, OutboxEvent
from radar_ingestion.normalizer import AlertSource, compute_fingerprint, normalize
from radar_ingestion.publisher import (
    RESOLVE_IGNORED_AUDIT_EVENT,
    persist_alert,
)
from sqlalchemy import func, select

SERVICE = "order-service"
ALERT_NAME = "OrderFailure"
SEVERITY = "critical"
T0 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)


def _alert(*, status: str, correlation_id: UUID | None = None) -> NormalizedAlert:
    """A mock-source normalized alert with the given source status."""
    return normalize(
        AlertSource.MOCK,
        {
            "service_name": SERVICE,
            "alert_name": ALERT_NAME,
            "severity": SEVERITY,
            "status": status,
            "fired_at": T0.isoformat(),
        },
        correlation_id=correlation_id,
    )


async def _counts(db: Database) -> tuple[int, int, int, int]:
    """Return ``(incidents, alerts, outbox_events, audit_rows)`` across the DB."""
    async with db.session() as session:
        incidents = await session.scalar(select(func.count()).select_from(Incident))
        alerts = await session.scalar(select(func.count()).select_from(Alert))
        events = await session.scalar(select(func.count()).select_from(OutboxEvent))
        audits = await session.scalar(select(func.count()).select_from(AuditLog))
    return incidents or 0, alerts or 0, events or 0, audits or 0


async def test_resolved_alert_no_open_incident_opens_nothing(db: Database) -> None:
    """The load-bearing guard: resolve with no match writes only an audit row.

    Delete the ``status == resolved and existing is None`` branch in
    ``persist_alert`` and this goes red: the resolve falls through to the create
    path, producing ``(1, 1, 1, 0)`` — a phantom incident, its alert, and the
    ``alert.normalized`` event that wakes the watcher — instead of ``(0, 0, 0, 1)``.
    """
    resolve = _alert(status="resolved")

    async with db.session() as session:
        result = await persist_alert(session, resolve)
        await session.commit()

    # The DB-state guarantee is asserted FIRST, before any flag on the result, so
    # the mutation (guard deleted) reddens THIS three-way-zero assertion rather
    # than an earlier `result.ignored` check that would shadow it. No incident, no
    # alert, no outbox event — and exactly one audit receipt.
    assert await _counts(db) == (0, 0, 0, 1)
    # The result mirrors that DB state: nothing landed, so there is no incident id.
    assert result.ignored is True
    assert result.incident_id is None
    assert result.deduplicated is False


async def test_resolve_ignored_audit_row_is_a_faithful_receipt(db: Database) -> None:
    """The one row written carries what "did RADAR receive this resolve?" needs.

    The audit row is the ONLY trace, so its identity fields have to be right: the
    fingerprint that was searched, the alert's own id and correlation id, and the
    reason. ``entity_type`` is ``alert`` because there is no incident to name.
    """
    ingress = uuid4()
    resolve = _alert(status="resolved", correlation_id=ingress)

    async with db.session() as session:
        await persist_alert(session, resolve)
        await session.commit()

    async with db.session() as session:
        row = (
            await session.execute(
                select(
                    AuditLog.event_type,
                    AuditLog.entity_type,
                    AuditLog.entity_id,
                    AuditLog.correlation_id,
                    AuditLog.actor,
                    AuditLog.payload,
                )
            )
        ).one()

    event_type, entity_type, entity_id, correlation_id, actor, payload = row
    assert event_type == RESOLVE_IGNORED_AUDIT_EVENT
    assert entity_type == "alert"
    assert entity_id == resolve.id
    assert correlation_id == ingress
    assert actor == "ingestion"
    assert payload["fingerprint"] == compute_fingerprint(SERVICE, ALERT_NAME, SEVERITY)
    assert payload["service_name"] == SERVICE
    assert payload["reason"] == "no_open_incident_for_resolved_alert"


async def test_firing_alert_with_no_match_still_opens_an_incident(db: Database) -> None:
    """Control: the guard is scoped to ``resolved``, not to "no open match".

    A firing alert with nothing to dedup onto must still take the create path —
    one incident, one alert, one event, and no ignore audit. If this regresses,
    the guard has over-reached and started swallowing real incidents.
    """
    firing = _alert(status="firing")

    async with db.session() as session:
        result = await persist_alert(session, firing)
        await session.commit()

    assert result.ignored is False
    assert result.incident_id is not None
    assert await _counts(db) == (1, 1, 1, 0)
