"""Outbox lifecycle tests: dispatch, retry backoff, and dead-lettering."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from radar_common import utcnow
from radar_database import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    STATUS_DEAD_LETTER,
    STATUS_PENDING,
    STATUS_PROCESSING,
    AuditLog,
    Database,
    OutboxEvent,
    claim_outbox_batch,
    mark_dispatched,
    mark_failed,
    write_outbox_event,
)
from sqlalchemy import func, select


async def _seed_one(db: Database, correlation_id: UUID) -> None:
    async with db.session() as session:
        await write_outbox_event(
            session,
            event_type="alert.normalized",
            target_service="watcher-agent",
            payload={},
            correlation_id=correlation_id,
        )
        await session.commit()


async def test_claim_marks_processing(db: Database) -> None:
    cid = uuid4()
    await _seed_one(db, cid)
    async with db.session() as session:
        claimed = await claim_outbox_batch(session, limit=10)
        assert len(claimed) == 1
        assert claimed[0].status == STATUS_PROCESSING
        await session.commit()


async def test_dispatch_deletes_event(db: Database) -> None:
    cid = uuid4()
    await _seed_one(db, cid)
    async with db.session() as session:
        (event,) = await claim_outbox_batch(session, limit=10)
        await mark_dispatched(session, event)
        await session.commit()
    async with db.session() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.correlation_id == cid)
        )
    assert remaining == 0


async def test_mark_failed_schedules_backoff(db: Database) -> None:
    cid = uuid4()
    await _seed_one(db, cid)
    async with db.session() as session:
        (event,) = await claim_outbox_batch(session, limit=10)
        now = utcnow()
        dead = await mark_failed(session, event, error="boom", now=now)
        assert dead is False
        assert event.status == STATUS_PENDING
        assert event.attempts == 1
        # Next attempt (2) is delayed by the first backoff step.
        assert event.process_after == now + timedelta(seconds=RETRY_DELAYS_SECONDS[2])
        assert event.last_error == "boom"
        await session.commit()


async def test_mark_failed_defaults_to_server_clock(db: Database) -> None:
    """Without an injected ``now``, backoff is scheduled off the Postgres clock.

    ``now()`` is the transaction timestamp (stable within a transaction), so a
    same-transaction ``now()`` equals the base ``mark_failed`` used — proving the
    schedule references the server clock, never the app host's. Also exercises
    re-attaching a detached (claimed-then-committed) row before marking it.
    """
    cid = uuid4()
    await _seed_one(db, cid)
    async with db.session() as session:
        (claimed,) = await claim_outbox_batch(session, limit=10)
        await session.commit()

    async with db.session() as session:
        event = await session.get(OutboxEvent, claimed.id)
        assert event is not None
        dead = await mark_failed(session, event, error="boom")
        assert dead is False
        server_now = await session.scalar(select(func.now()))
        assert isinstance(server_now, datetime)  # NOW() returns one timestamptz row
        assert event.process_after == server_now + timedelta(
            seconds=RETRY_DELAYS_SECONDS[2]
        )
        await session.commit()


async def test_dead_letter_after_max_attempts(db: Database) -> None:
    cid = uuid4()
    async with db.session() as session:
        event = await write_outbox_event(
            session,
            event_type="alert.normalized",
            target_service="watcher-agent",
            payload={},
            correlation_id=cid,
        )
        dead = False
        for _ in range(MAX_ATTEMPTS):
            dead = await mark_failed(session, event, error="boom")
        await session.commit()

    assert dead is True
    assert event.status == STATUS_DEAD_LETTER
    assert event.attempts == MAX_ATTEMPTS

    async with db.session() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.correlation_id == cid)
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].event_type == "outbox.dead_letter"
