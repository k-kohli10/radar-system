"""Idempotency across a worker crash: at-least-once delivery, exactly-once processing.

The hazard model A creates: the worker commits its claim *before* dispatching, so
a worker that dies after the target has already processed the event but before it
marks the event delivered leaves the row stranded in ``processing`` — with the
event **already delivered**. The reaper cannot know that. It re-pends the row, a
new worker claims it, and the target is dispatched the *same event a second time*.
Re-dispatch is not a bug to be prevented; it is guaranteed by at-least-once
delivery.

So "delivered exactly once" is a **joint** guarantee — worker at-least-once plus
the *target's* ``processed_events`` check — not a property the worker can hold on
its own. These tests prove the composition end to end. ``packages/database`` pins
the dedup primitive in isolation; nothing until now exercised
crash -> reaper -> re-dispatch -> target-dedup as a flow.

**How the crash is simulated, and why.** The hazard is defined by a *database
state* — claim committed, dispatch completed, mark never committed — not by a
particular mechanism of death. So we reproduce that state deterministically: do
the real claim, do the real dispatch, then simply never run the mark (exactly what
a dead process does). A ``task.cancel()`` or a SIGKILL'd process fixture would
produce the same row state, but only if the kill lands in the window *after* the
target processed and *before* the mark commits — which is a race you cannot pin.
Such a test would be flaky, and worse, could silently drift into killing at some
other point, still passing while no longer exercising the hazard at all. A
theatrical kill tests the scheduler; the DB end-state tests the thing.

**What is faked, and what is not.** The receiver below fakes only the HTTP shell
of an agent's ``POST /events`` (the real agents arrive in Phase 7). The dedup it
performs is the real :func:`radar_database.is_already_processed` /
:func:`radar_database.mark_processed` against real Postgres — the exact contract
the plan requires of every agent ("Check processed_events -> if seen, return
200"). We fake the thing that does not exist yet and test the thing that does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from radar_database import (
    MAX_ATTEMPTS,
    STATUS_DEAD_LETTER,
    STATUS_PENDING,
    STATUS_PROCESSING,
    AuditLog,
    Database,
    OutboxEvent,
    ProcessedEvent,
    claim_outbox_batch,
    is_already_processed,
    mark_processed,
    write_outbox_event,
)
from radar_outbox_worker.dead_letter import Reaper
from radar_outbox_worker.dispatcher import EventDispatcher, TargetResolver
from radar_outbox_worker.poller import Poller
from radar_outbox_worker.retry import DispatchProcessor
from sqlalchemy import func, select, update

TARGET_SERVICE = "watcher-agent"
REAPER_INTERVAL_SECONDS = 60  # the real default; rows are aged past it, not tuned down
STUCK_AGE_SECONDS = 120


class IdempotentTarget(httpx.AsyncBaseTransport):
    """A stand-in for a Phase 7 agent's ``POST /events``.

    Fakes only the HTTP shell. The idempotency check is the real
    ``processed_events`` contract every agent must run, against real Postgres:
    if the event was already handled, return 200 without re-doing the work.

    ``received`` counts every dispatch that arrived (including duplicates);
    ``handled`` counts the ones that actually did work.
    """

    def __init__(self, database: Database, service_name: str) -> None:
        self._database = database
        self._service = service_name
        self.received: list[UUID] = []
        self.handled: list[UUID] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        event_id = UUID(json.loads(request.content)["event_id"])
        self.received.append(event_id)
        async with self._database.session() as session:
            if await is_already_processed(session, event_id, self._service):
                return httpx.Response(200)  # already handled — idempotent no-op
            self.handled.append(event_id)  # the actual work happens exactly here
            await mark_processed(session, event_id, self._service)
            await session.commit()
        return httpx.Response(200)


async def _seed(db: Database) -> tuple[UUID, UUID]:
    """Write one pending event; return ``(row_id, event_id)``."""
    async with db.session() as session:
        event = await write_outbox_event(
            session,
            event_type="alert.normalized",
            target_service=TARGET_SERVICE,
            payload={},
            correlation_id=uuid4(),
        )
        await session.commit()
        return event.id, event.event_id


async def _claim_one(db: Database) -> OutboxEvent | None:
    async with db.session() as session:
        claimed = await claim_outbox_batch(session, limit=10)
        await session.commit()
    return claimed[0] if claimed else None


async def _row(db: Database, row_id: UUID) -> OutboxEvent | None:
    async with db.session() as session:
        return await session.get(OutboxEvent, row_id)


async def _require_row(db: Database, row_id: UUID) -> OutboxEvent:
    row = await _row(db, row_id)
    assert row is not None
    return row


async def _age_row(db: Database, row_id: UUID, *, seconds: int) -> None:
    """Backdate ``updated_at`` so the row looks stuck past the reaper threshold.

    Simulates the row sitting there while a crashed worker stays down, so the
    reaper runs at its real interval instead of being tuned down to zero.
    """
    async with db.session() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == row_id)
            .values(updated_at=func.now() - timedelta(seconds=seconds))
        )
        await session.commit()


async def _reap_until(
    db: Database,
    row_id: UUID,
    predicate: Callable[[OutboxEvent], bool],
    *,
    timeout: float = 10.0,
) -> OutboxEvent:
    """Run the real Reaper loop until ``predicate`` holds for the row, then stop."""
    reaper = Reaper(db, interval_seconds=REAPER_INTERVAL_SECONDS)
    task = asyncio.create_task(reaper.run())
    try:
        async with asyncio.timeout(timeout):
            while True:
                row = await _require_row(db, row_id)
                if predicate(row):
                    return row
                await asyncio.sleep(0.02)
    finally:
        reaper.stop()
        await task


async def _processed_count(db: Database, event_id: UUID) -> int:
    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(
                ProcessedEvent.event_id == event_id,
                ProcessedEvent.processed_by == TARGET_SERVICE,
            )
        )
    return int(count or 0)


async def _outbox_count(db: Database) -> int:
    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    return int(count or 0)


async def test_crash_before_mark_redelivers_and_target_dedups_to_exactly_once(
    db: Database,
) -> None:
    """Crash after delivery, before marking: the event is re-dispatched, and the
    target's processed_events check absorbs the duplicate — processed exactly once.
    """
    row_id, event_id = await _seed(db)
    target = IdempotentTarget(db, TARGET_SERVICE)
    client = httpx.AsyncClient(transport=target)
    dispatcher = EventDispatcher(client, TargetResolver(), SecretStr("t" * 64))

    try:
        # --- Worker A: real claim, real dispatch, then die before marking. ---
        event = await _claim_one(db)
        assert event is not None
        result = await dispatcher.dispatch(event)
        assert result.delivered, "the target accepted and processed the event"
        # Worker A dies HERE. mark_dispatched never runs.

        stranded = await _require_row(db, row_id)
        assert stranded.status == STATUS_PROCESSING, "row stranded by the crash"
        assert stranded.attempts == 0, "the crash itself records no attempt"
        assert target.handled == [event_id], "target did the work once, pre-crash"

        # --- Restart: the row has sat stuck past the reaper's threshold. ---
        await _age_row(db, row_id, seconds=STUCK_AGE_SECONDS)
        recovered = await _reap_until(
            db, row_id, lambda r: r.status != STATUS_PROCESSING
        )
        assert recovered.status == STATUS_PENDING, "reaper re-pends the stranded row"
        assert recovered.attempts == 1, "the reaper counts the crash as an attempt"

        # --- Worker B (the restarted worker): claims and RE-dispatches. ---
        poller = Poller(
            db, DispatchProcessor(db, dispatcher), poll_interval_seconds=0.01
        )
        task = asyncio.create_task(poller.run())
        try:
            async with asyncio.timeout(10.0):
                while await _outbox_count(db) > 0:
                    await asyncio.sleep(0.02)
        finally:
            poller.stop()
            await task
    finally:
        await client.aclose()

    # The duplicate dispatch genuinely happened. Without this, the test is
    # vacuous: if redelivery never occurred, the dedup was never exercised and
    # "exactly once" would only be luck.
    assert len(target.received) == 2, "the event must have been dispatched twice"
    assert target.received[0] == target.received[1] == event_id, (
        "both dispatches must be the SAME event — otherwise dedup is being "
        "credited for a collision between different events"
    )

    # THE PROOF: at-least-once delivery, exactly-once processing. The second
    # dispatch was absorbed by the target's processed_events check.
    assert target.handled == [event_id], "the target must process the event ONCE"
    assert await _processed_count(db, event_id) == 1

    # Nothing lost: the event reached its terminal delivered state.
    assert await _row(db, row_id) is None, "delivered event is removed from the outbox"


async def test_repeated_crashes_terminate_in_dead_letter_not_an_infinite_loop(
    db: Database,
) -> None:
    """A crash-loop must terminate, and this is what welds Decision A to behaviour.

    The reaper increments ``attempts`` on every recovery *precisely so* that an
    event which keeps killing its worker mid-dispatch still reaches a terminal
    state instead of being re-pended forever. If someone later "fixes" the reaper
    to re-pend for free — reasoning that a crash is not the event's fault — this
    test stops terminating and says exactly why: attempts never climbs, the
    ceiling is never reached, and the event loops until someone notices.

    Five crash-and-recover cycles must walk attempts 1..5 and end in dead_letter.
    """
    row_id, event_id = await _seed(db)
    target = IdempotentTarget(db, TARGET_SERVICE)
    client = httpx.AsyncClient(transport=target)
    dispatcher = EventDispatcher(client, TargetResolver(), SecretStr("t" * 64))

    try:
        for cycle in range(1, MAX_ATTEMPTS + 1):
            event = await _claim_one(db)
            assert event is not None, f"event must still be claimable on cycle {cycle}"
            result = await dispatcher.dispatch(event)
            assert result.delivered
            # Crash again: mark never runs, row stranded in 'processing'.

            await _age_row(db, row_id, seconds=STUCK_AGE_SECONDS)
            row = await _reap_until(db, row_id, lambda r: r.status != STATUS_PROCESSING)

            if cycle < MAX_ATTEMPTS:
                # Still recoverable — but the attempt is COUNTED. This per-cycle
                # assertion is what a free re-pend would fail on immediately.
                assert row.status == STATUS_PENDING
                assert row.attempts == cycle, (
                    f"cycle {cycle}: attempts must climb, or the loop never ends"
                )
            else:
                assert row.status == STATUS_DEAD_LETTER, (
                    "the crash-loop must terminate at the ceiling, not spin forever"
                )
                assert row.attempts == MAX_ATTEMPTS
    finally:
        await client.aclose()

    # Terminal: a dead-lettered row is never claimed again — the loop is over.
    final = await _require_row(db, row_id)
    assert final.status == STATUS_DEAD_LETTER
    assert await _claim_one(db) is None, "dead-lettered event must not be re-claimed"

    async with db.session() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.event_type == "outbox.dead_letter")
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].payload["trigger"] == "exhausted"

    # Dedup held throughout: five dispatches, one unit of work.
    assert len(target.received) == MAX_ATTEMPTS, "every cycle re-dispatched"
    assert target.handled == [event_id], "the target processed the event exactly once"
    assert await _processed_count(db, event_id) == 1
