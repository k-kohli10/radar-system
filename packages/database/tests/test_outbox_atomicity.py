"""Critical test 1: transactional outbox atomicity.

Insert an incident and its outbox event in one transaction, raise before commit,
and verify neither row exists — a state change and the event it produces are
all-or-nothing.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from radar_database import Database, Incident, OutboxEvent, write_outbox_event
from sqlalchemy import func, select


class _BoomError(Exception):
    """Sentinel error raised mid-transaction, before commit."""


async def test_incident_and_outbox_roll_back_together(db: Database) -> None:
    correlation_id = uuid4()

    with pytest.raises(_BoomError):
        async with db.session() as session:
            session.add(
                Incident(
                    correlation_id=correlation_id,
                    fingerprint="fp",
                    service_name="order-service",
                    title="Orders failing",
                    severity="critical",
                )
            )
            await write_outbox_event(
                session,
                event_type="incident.plan_requested",
                target_service="planner-agent",
                payload={"correlation_id": str(correlation_id)},
                correlation_id=correlation_id,
            )
            # Both rows are flushed but NOT committed; the exception must roll
            # the whole transaction back.
            raise _BoomError

    async with db.session() as session:
        incidents = await session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.correlation_id == correlation_id)
        )
        events = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.correlation_id == correlation_id)
        )

    assert incidents == 0
    assert events == 0
