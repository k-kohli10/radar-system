"""Matched resolves flip firing alerts to resolved (real Postgres).

Commit 3's alert-level state machine: a ``resolved`` alert that matches a LIVE
incident (``open`` or ``investigating``, unwindowed) flips that incident's still-
firing alert rows to ``resolved`` at the webhook time — and touches nothing else.
The incident row is untouched (its own transition is the next commit's gate), no
new alert row is written, ``alert_count`` is not bumped, and no ``alert.normalized``
outbox event is published.

Two guarantees carry weight here:

- **The predicate choice is load-bearing, not asserted.** ``test_resolve_matches_
  investigating_incident_opened_outside_window`` drives the exact case the dedup
  lookup would have missed — an incident that is ``investigating`` AND opened well
  outside the 5-minute window — which is the COMMON case (alerts clear long after
  they fire, after the RCA card was sent). If resolution reused dedup's windowed,
  open-only lookup this resolve would be mistaken for "no incident" and the alert
  would never resolve.
- **Redelivery is idempotent via the firing predicate.** Alertmanager retries, so
  the same resolve arrives twice; the second must NOT move ``resolved_at`` forward.
  ``test_duplicate_resolve_does_not_move_resolved_at`` is mutation-guarded: drop the
  ``status = 'firing'`` predicate and the duplicate re-stamps ``resolved_at``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from radar_contracts import NormalizedAlert
from radar_database import Alert, AuditLog, Database, Incident, OutboxEvent
from radar_ingestion.normalizer import AlertSource, compute_fingerprint, normalize
from radar_ingestion.publisher import persist_alert
from radar_ingestion.resolver import mark_incident_alerts_resolved
from sqlalchemy import func, select

SERVICE = "order-service"
ALERT_NAME = "OrderFailure"
SEVERITY = "critical"
FINGERPRINT = compute_fingerprint(SERVICE, ALERT_NAME, SEVERITY)
T0 = datetime(2026, 7, 9, 10, 0, 0, tzinfo=UTC)


def _resolve(*, ended_at: datetime) -> NormalizedAlert:
    """A resolved prometheus alert (``endsAt`` -> ``resolved_at``)."""
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


async def _seed_incident(
    db: Database, *, status: str, opened_at: datetime, alert_count: int = 1
) -> UUID:
    """Commit an incident in ``status`` opened at ``opened_at`` with firing alerts.

    Built directly (not via persist_alert) so ``opened_at`` can be placed outside
    the dedup window and the status set to ``investigating`` — the state the real
    pipeline reaches before a resolve arrives.
    """
    incident_id = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint=FINGERPRINT,
                service_name=SERVICE,
                title=f"{SERVICE} {ALERT_NAME}",
                severity=SEVERITY,
                status=status,
                alert_count=alert_count,
                opened_at=opened_at,
            )
        )
        for _ in range(alert_count):
            session.add(
                Alert(
                    id=uuid4(),
                    source="prometheus",
                    fingerprint=FINGERPRINT,
                    service_name=SERVICE,
                    alert_name=ALERT_NAME,
                    severity=SEVERITY,
                    status="firing",
                    raw_payload={},
                    fired_at=opened_at,
                    incident_id=incident_id,
                    correlation_id=uuid4(),
                )
            )
        await session.commit()
    return incident_id


async def _alerts_for(db: Database, incident_id: UUID) -> list[Alert]:
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


async def _incident(db: Database, incident_id: UUID) -> Incident:
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    return incident


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --- the reason the predicate exists ---------------------------------------------


async def test_resolve_matches_investigating_incident_opened_outside_window(
    db: Database,
) -> None:
    """The exact case dedup's windowed, open-only lookup would MISS.

    The incident is ``investigating`` (RCA card sent) and opened two hours ago —
    far outside the 5-minute dedup window. This is the common case, not an edge:
    alerts clear long after they fire. The resolve must find it, flip its firing
    alert, and — its last firing alert now cleared — resolve the incident
    (``investigating -> resolved``, ADR 0016 Amendment 2). If resolution used
    ``find_open_incident`` this alert would be treated as "no incident" and never
    resolve — the whole reason resolver.py exists.
    """
    opened = T0 - timedelta(hours=2)
    incident_id = await _seed_incident(db, status="investigating", opened_at=opened)
    ended = T0
    await _persist(db, _resolve(ended_at=ended))

    alerts = await _alerts_for(db, incident_id)
    assert len(alerts) == 1
    assert alerts[0].status == "resolved"
    assert alerts[0].resolved_at == ended

    # Its last firing alert cleared, so the incident resolves, stamped with the
    # webhook time; alert_count is not bumped (a resolve is not a new firing).
    incident = await _incident(db, incident_id)
    assert incident.status == "resolved"
    assert incident.resolved_at == ended
    assert incident.alert_count == 1


# --- the flip is bounded to the incident's firing alerts -------------------------


async def test_matched_resolve_adds_no_alert_row_and_no_outbox_event(
    db: Database,
) -> None:
    """A matched resolve flips existing rows and resolves the incident — and does
    so without an INSERT or an outbox event.

    No new alert row (a resolve is a transition of existing rows, not a new alert),
    no ``alert_count`` bump, and no ``alert.normalized`` outbox event (the watcher
    escalates on arrival rate and must not see a resolve). The incident DOES resolve
    — its one firing alert cleared — but that is a status UPDATE, not a new row or
    an event.
    """
    incident_id = await _seed_incident(db, status="open", opened_at=T0)
    events_before = await _count(db, OutboxEvent)
    alerts_before = await _count(db, Alert)

    ended = T0 + timedelta(minutes=30)
    await _persist(db, _resolve(ended_at=ended))

    # Exactly the one firing alert flipped; no row added.
    assert await _count(db, Alert) == alerts_before
    alerts = await _alerts_for(db, incident_id)
    assert [a.status for a in alerts] == ["resolved"]
    assert alerts[0].resolved_at == ended
    # No outbox event from the resolve.
    assert await _count(db, OutboxEvent) == events_before
    # Incident resolved (last firing alert cleared), count not bumped.
    incident = await _incident(db, incident_id)
    assert incident.status == "resolved"
    assert incident.alert_count == 1


async def test_multi_alert_incident_flips_all_firing_rows(db: Database) -> None:
    """A resolve flips EVERY firing alert on the incident, not just one.

    Sets up commit 4's gate: an incident can carry several firing alert rows (dedup
    attaches duplicates). One resolve covers them all — so after it, no firing alert
    remains, which is the condition commit 4 will read to decide the incident.
    """
    incident_id = await _seed_incident(
        db, status="investigating", opened_at=T0, alert_count=3
    )
    ended = T0 + timedelta(minutes=10)
    await _persist(db, _resolve(ended_at=ended))

    alerts = await _alerts_for(db, incident_id)
    assert len(alerts) == 3
    assert all(a.status == "resolved" for a in alerts)
    assert all(a.resolved_at == ended for a in alerts)


# --- redelivery idempotency ------------------------------------------------------


async def test_mark_incident_alerts_resolved_is_idempotent(db: Database) -> None:
    """The firing-predicate short-circuit, tested on the function directly.

    ``mark_incident_alerts_resolved`` is called twice on the same incident with a
    LATER webhook time the second time. The ``status = 'firing'`` predicate means
    the second call matches nothing (the rows are already ``resolved``) and leaves
    ``resolved_at`` at the first value.

    Driven directly, not through ``persist_alert``: once the incident transitions to
    ``resolved`` (commit 4), a redelivered webhook is turned away at the incident
    lookup and never reaches this function — so the end-to-end redelivery test below
    no longer exercises this predicate. This one keeps it mutation-proven in
    isolation. MUTATION: drop the ``status = 'firing'`` predicate → the second call
    re-stamps ``resolved_at`` to the later time and the final assertion goes red.
    """
    incident_id = await _seed_incident(
        db, status="investigating", opened_at=T0, alert_count=2
    )
    first = T0 + timedelta(minutes=5)
    later = T0 + timedelta(minutes=45)

    async with db.session() as session:
        flipped = await mark_incident_alerts_resolved(
            session, incident_id=incident_id, resolved_at=first
        )
        await session.commit()
    assert flipped == 2

    async with db.session() as session:
        flipped_again = await mark_incident_alerts_resolved(
            session, incident_id=incident_id, resolved_at=later
        )
        await session.commit()
    assert flipped_again == 0  # nothing still firing to flip

    alerts = await _alerts_for(db, incident_id)
    assert all(a.status == "resolved" for a in alerts)
    assert all(a.resolved_at == first for a in alerts)


async def test_duplicate_resolve_webhook_leaves_incident_resolved_once(
    db: Database,
) -> None:
    """End-to-end redelivery: a retried resolve does not move anything.

    The first webhook flips the alerts and resolves the incident. The second is
    turned away at the (locked, non-terminal-only) lookup — the incident is now
    ``resolved`` — so it writes only an ignore audit, moves no ``resolved_at``, and
    produces no second ``incident.resolved`` row. Idempotency at the incident level,
    complementing the alert-level short-circuit above.
    """
    incident_id = await _seed_incident(
        db, status="investigating", opened_at=T0, alert_count=2
    )
    first_ended = T0 + timedelta(minutes=5)
    second_ended = T0 + timedelta(minutes=45)

    await _persist(db, _resolve(ended_at=first_ended))
    await _persist(db, _resolve(ended_at=second_ended))

    alerts = await _alerts_for(db, incident_id)
    assert all(a.resolved_at == first_ended for a in alerts)

    incident = await _incident(db, incident_id)
    assert incident.status == "resolved"
    assert incident.resolved_at == first_ended

    async with db.session() as session:
        resolved_audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "incident.resolved")
                )
            )
            .scalars()
            .all()
        )
    assert len(resolved_audits) == 1  # exactly one transition, not two


async def test_resolved_alert_no_live_incident_still_ignored(db: Database) -> None:
    """Control: with no live incident, a resolve is still recorded and ignored.

    Preserves commit 1's guarantee under the new unwindowed lookup — no incident
    seeded, so find_resolvable_incident returns None and the resolve writes only an
    audit row.
    """
    await _persist(db, _resolve(ended_at=T0))

    assert await _count(db, Incident) == 0
    assert await _count(db, Alert) == 0
    assert await _count(db, OutboxEvent) == 0
    async with db.session() as session:
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert len(audits) == 1
    assert audits[0].event_type == "ingestion.resolve_ignored"


async def test_resolve_ignores_terminal_incident(db: Database) -> None:
    """A resolve does not match an already-resolved (terminal) incident.

    ``find_resolvable_incident`` spans only ``{open, investigating}``. An incident
    already ``resolved`` is terminal for this purpose, so a stray later resolve for
    its fingerprint matches nothing and is ignored — it does not re-open or re-touch
    the closed-out episode.
    """
    await _seed_incident(db, status="resolved", opened_at=T0)

    await _persist(db, _resolve(ended_at=T0 + timedelta(minutes=10)))

    async with db.session() as session:
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert [a.event_type for a in audits] == ["ingestion.resolve_ignored"]
