"""Dead-letter promotion: the exact retry ceiling, and the permanent short-circuit.

Two promotion paths reach ``dead_letter``, and both must funnel through the one
shared ``_promote_to_dead_letter`` helper so every dead-letter is recorded
identically:

- **Exhausted** — a transient failure (503) retried until the ceiling. The
  boundary is inclusive-edge precise and pinned rung by rung: the event stays
  ``pending`` through four failures (each rescheduled with the exact spec
  backoff — 5s, 15s, 60s, 300s), and is promoted on the **fifth** failure
  (``attempts == MAX_ATTEMPTS == 5``). Not earlier, and never a sixth dispatch:
  a dead-lettered row is not claimable again.
- **Permanent** — a 422 will never succeed on retry, so it is dead-lettered on
  first sight (``attempts == 1``), without burning the retry budget.

Real Postgres throughout: the backoff timestamps are server-clock values and the
claim is a real ``FOR UPDATE SKIP LOCKED`` claim. Backoff delays are simulated by
making the row due again rather than sleeping 300 seconds.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from radar_database import (
    MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    STATUS_DEAD_LETTER,
    STATUS_PENDING,
    AuditLog,
    Database,
    OutboxEvent,
    claim_outbox_batch,
    write_outbox_event,
)
from radar_outbox_worker.dispatcher import EventDispatcher, TargetResolver
from radar_outbox_worker.retry import DispatchProcessor
from sqlalchemy import func, select, update
from tokens import token_map

DEAD_LETTER_AUDIT = "outbox.dead_letter"


def _processor(
    db: Database, status_code: int
) -> tuple[DispatchProcessor, httpx.AsyncClient]:
    """A real dispatch path whose target always answers ``status_code``."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code))
    )
    dispatcher = EventDispatcher(client, TargetResolver(), token_map())
    return DispatchProcessor(db, dispatcher), client


async def _seed(db: Database) -> UUID:
    """Write one pending event; return its row id."""
    async with db.session() as session:
        event = await write_outbox_event(
            session,
            event_type="alert.normalized",
            target_service="watcher-agent",
            payload={},
            correlation_id=uuid4(),
        )
        await session.commit()
        return event.id


async def _claim_one(db: Database) -> OutboxEvent | None:
    async with db.session() as session:
        claimed = await claim_outbox_batch(session, limit=10)
        await session.commit()
    return claimed[0] if claimed else None


async def _row(db: Database, row_id: UUID) -> OutboxEvent:
    async with db.session() as session:
        row = await session.get(OutboxEvent, row_id)
    assert row is not None
    return row


async def _make_due(db: Database, row_id: UUID) -> None:
    """Simulate the backoff elapsing (rather than sleeping up to 300s)."""
    async with db.session() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row_id)
            .values(process_after=func.now())
        )
        await session.commit()


async def _dead_letter_audits(db: Database) -> list[AuditLog]:
    async with db.session() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.event_type == DEAD_LETTER_AUDIT)
        )
        return list(result.scalars().all())


async def test_transient_failures_dead_letter_on_the_fifth_not_before(
    db: Database,
) -> None:
    """Alive through four failures; promoted on the fifth. No sixth dispatch."""
    row_id = await _seed(db)
    processor, client = _processor(db, 503)  # transient: retryable
    try:
        # Rungs 1..4: each failure reschedules with the exact spec backoff and
        # must NOT dead-letter.
        for attempt in range(1, MAX_ATTEMPTS):
            event = await _claim_one(db)
            assert event is not None, f"event must be claimable for attempt {attempt}"
            await processor(event)

            row = await _row(db, row_id)
            assert row.status == STATUS_PENDING, (
                f"failure {attempt} of {MAX_ATTEMPTS} must not dead-letter yet"
            )
            assert row.attempts == attempt
            # process_after and updated_at are set from the same server moment,
            # so their difference is exactly the scheduled backoff.
            expected = timedelta(seconds=RETRY_DELAYS_SECONDS[attempt + 1])
            assert row.process_after - row.updated_at == expected, (
                f"attempt {attempt + 1} must be scheduled {expected} out"
            )
            assert not await _dead_letter_audits(db), "no audit before the ceiling"

            await _make_due(db, row_id)

        # The fifth failure crosses the ceiling.
        event = await _claim_one(db)
        assert event is not None, "event must still be claimable for its fifth attempt"
        await processor(event)
    finally:
        await client.aclose()

    row = await _row(db, row_id)
    assert row.status == STATUS_DEAD_LETTER, "the fifth failure must dead-letter"
    assert row.attempts == MAX_ATTEMPTS == 5

    audits = await _dead_letter_audits(db)
    assert len(audits) == 1, "exactly one dead-letter audit record"
    assert audits[0].payload["trigger"] == "exhausted"
    assert audits[0].payload["attempts"] == MAX_ATTEMPTS

    # And there is never a sixth dispatch: a dead-lettered row is not claimable,
    # even once its process_after is due.
    await _make_due(db, row_id)
    assert await _claim_one(db) is None, (
        "a dead-lettered event must never be claimed again"
    )


async def test_permanent_failure_dead_letters_on_first_sight(db: Database) -> None:
    """A 422 never succeeds on retry: promoted immediately, not at the ceiling."""
    row_id = await _seed(db)
    processor, client = _processor(db, 422)  # permanent
    try:
        event = await _claim_one(db)
        assert event is not None
        await processor(event)
    finally:
        await client.aclose()

    row = await _row(db, row_id)
    assert row.status == STATUS_DEAD_LETTER, "permanent failure must not wait"
    assert row.attempts == 1, "dead-lettered on the first failure, not the fifth"
    assert row.attempts < MAX_ATTEMPTS

    audits = await _dead_letter_audits(db)
    assert len(audits) == 1
    assert audits[0].payload["trigger"] == "permanent"

    # Not claimable again either.
    await _make_due(db, row_id)
    assert await _claim_one(db) is None


async def test_both_promotion_paths_write_one_audit_shape(db: Database) -> None:
    """Exhausted and permanent records differ only in ``trigger``.

    Both are written by the single ``_promote_to_dead_letter`` helper, so their
    audit payloads have identical keys. A second, divergent promotion path would
    show up here as a shape mismatch.
    """
    # Permanent (422) — one failure.
    perm_row = await _seed(db)
    processor, client = _processor(db, 422)
    try:
        event = await _claim_one(db)
        assert event is not None
        await processor(event)
    finally:
        await client.aclose()

    # Exhausted (503) — driven to the ceiling.
    exh_row = await _seed(db)
    processor, client = _processor(db, 503)
    try:
        for _ in range(MAX_ATTEMPTS):
            event = await _claim_one(db)
            assert event is not None
            await processor(event)
            await _make_due(db, exh_row)
    finally:
        await client.aclose()

    assert (await _row(db, perm_row)).status == STATUS_DEAD_LETTER
    assert (await _row(db, exh_row)).status == STATUS_DEAD_LETTER

    audits = await _dead_letter_audits(db)
    assert len(audits) == 2
    by_trigger = {a.payload["trigger"]: a for a in audits}
    assert set(by_trigger) == {"permanent", "exhausted"}

    permanent, exhausted = by_trigger["permanent"], by_trigger["exhausted"]
    # Same helper => same record shape, same audit event_type, only trigger differs.
    assert permanent.payload.keys() == exhausted.payload.keys()
    assert permanent.event_type == exhausted.event_type == DEAD_LETTER_AUDIT
    assert permanent.entity_type == exhausted.entity_type == "outbox_event"
    assert permanent.payload["attempts"] == 1
    assert exhausted.payload["attempts"] == MAX_ATTEMPTS
