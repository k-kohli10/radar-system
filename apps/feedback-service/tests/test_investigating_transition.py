"""Delivery moves the incident open -> investigating (ADR 0016 Amendment 1).

`investigating` means "a human has been told" — true exactly when the RCA card is
delivered, not when the recommendation row is written. So feedback-service makes
the transition, in the SAME transaction as the delivery record and AFTER the post.

Two guarantees:

- **The transition is gated on delivery.** A failed post leaves the incident
  `open`, never `investigating` — an incident sitting in `investigating` while the
  card is still in the outbox (or dead-lettered) is the lie the amendment exists to
  prevent. Because the transition is in the delivery transaction, a rolled-back
  delivery rolls back the transition. The mutation: move the transition ahead of
  the post, and a failed post strands the incident in `investigating`.

- **A stage-1 resolve that raced delivery is tolerated.** If the incident resolved
  between the reasoner writing the RCA and this delivery, `resolved -> investigating`
  is illegal; the card still delivers and the incident stays `resolved`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fakes import FakeNotifier
from radar_database import (
    AuditLog,
    Database,
    Incident,
    InvestigationPlan,
    Recommendation,
)
from radar_feedback_service.delivery import (
    DeliveryOutcome,
    NotificationFailedError,
    deliver_rca,
)
from sqlalchemy import func, select

CHANNEL = "#all-my-tech"
SERVICE_NAME = "feedback-service"


async def _seed(db: Database, *, incident_status: str = "open") -> tuple[UUID, UUID]:
    incident_id = uuid4()
    plan_id = uuid4()
    rec_id = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint="f" * 64,
                service_name="order-service",
                title="order-service OrderFailure",
                severity="high",
                status=incident_status,
            )
        )
        session.add(
            InvestigationPlan(
                id=plan_id,
                incident_id=incident_id,
                correlation_id=uuid4(),
                steps=[{"order": 1, "description": "check deploys"}],
            )
        )
        session.add(
            Recommendation(
                id=rec_id,
                incident_id=incident_id,
                plan_id=plan_id,
                correlation_id=uuid4(),
                llm_provider="openai",
                model_alias="extended",
                model_id="gpt-4o",
                root_cause="A bad deploy raised the pool timeout.",
                confidence="high",
                recommended_actions=[{"order": 1, "action": "Roll back."}],
            )
        )
        await session.commit()
    return incident_id, rec_id


async def _deliver(
    db: Database,
    notifier: FakeNotifier,
    *,
    incident_id: UUID,
    recommendation_id: UUID,
) -> DeliveryOutcome:
    async with db.session() as session:
        outcome = await deliver_rca(
            session,
            notifier,
            recommendation_id=recommendation_id,
            incident_id=incident_id,
            channel=CHANNEL,
            event_id=uuid4(),
            service_name=SERVICE_NAME,
        )
        await session.commit()
    return outcome


async def _status(db: Database, incident_id: UUID) -> str:
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    return incident.status


async def _investigating_audits(db: Database, incident_id: UUID) -> int:
    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_id == incident_id,
                AuditLog.event_type == "incident.investigating",
            )
        )
    return int(count or 0)


async def test_delivery_transitions_open_incident_to_investigating(
    db: Database,
) -> None:
    """The loop stage 1 left open: a delivered card moves the incident to
    investigating, with an incident.investigating audit row carrying the RCA."""
    incident_id, rec_id = await _seed(db, incident_status="open")

    outcome = await _deliver(
        db, FakeNotifier(), incident_id=incident_id, recommendation_id=rec_id
    )

    assert outcome is DeliveryOutcome.DELIVERED
    assert await _status(db, incident_id) == "investigating"
    assert await _investigating_audits(db, incident_id) == 1


async def test_failed_post_leaves_incident_open(db: Database) -> None:
    """The ordering guarantee for the transition: no card, no investigating.

    A failing post rolls back the whole transaction, including the transition, so
    the incident stays `open` and redelivery retries. The load-bearing property is
    that the transition shares the delivery's transaction: MUTATION — commit the
    transition BEFORE the post — and a failed post strands the incident in
    `investigating` with no card ever delivered, turning this assertion red.
    (Merely reordering the transition ahead of the post WITHIN the transaction is
    safe: the raise rolls it back with everything else. Only committing it early
    breaks the invariant, which is the mutation that matters.)
    """
    incident_id, rec_id = await _seed(db, incident_status="open")

    with pytest.raises(NotificationFailedError):
        await _deliver(
            db,
            FakeNotifier(fail=True),
            incident_id=incident_id,
            recommendation_id=rec_id,
        )

    assert await _status(db, incident_id) == "open"
    assert await _investigating_audits(db, incident_id) == 0


async def test_delivery_to_already_resolved_incident_still_posts_no_transition(
    db: Database,
) -> None:
    """A stage-1 resolve that beat delivery: card posts, incident stays resolved.

    resolved -> investigating is illegal; the transition is caught and skipped, the
    card is delivered anyway (the RCA is worth reading), and the incident is NOT
    dragged back to investigating.
    """
    incident_id, rec_id = await _seed(db, incident_status="resolved")
    notifier = FakeNotifier()

    outcome = await _deliver(
        db, notifier, incident_id=incident_id, recommendation_id=rec_id
    )

    assert outcome is DeliveryOutcome.DELIVERED
    assert len(notifier.calls) == 1  # card still posted
    assert await _status(db, incident_id) == "resolved"  # not investigating
    assert await _investigating_audits(db, incident_id) == 0
