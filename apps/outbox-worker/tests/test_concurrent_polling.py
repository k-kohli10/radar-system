"""Critical test: two concurrent pollers must never double-process an event.

This is the load-bearing guarantee of the phase. The outbox is at-least-once, and
the only thing standing between "at-least-once" and "the on-call engineer gets
two Slack pages for one incident" is the claim query's ``FOR UPDATE SKIP LOCKED``:
concurrent workers claim *disjoint* sets of rows.

What makes this test actually prove that, rather than merely pass:

- **Genuinely concurrent.** Two real ``Poller.run()`` loops are started as
  overlapping asyncio tasks and race for the same event set. Running one poller
  to completion and then the other would prove nothing — the second would have
  nothing left to double-claim.
- **Real Postgres.** ``SKIP LOCKED`` is Postgres row-locking behaviour. It does
  not exist in SQLite and a mock has no locking at all, so a non-Postgres run of
  this test is worthless. The suite skips when Postgres is unavailable.
- **Real contention.** 60 events, a fast poll interval, and a small per-event
  delay keep both pollers in flight simultaneously, so their claim transactions
  genuinely overlap and one must skip rows the other has locked.
- **Per-poller claim tracking.** Each poller records the ``event_id``s *it*
  claimed. A bare "total dispatches == 60" count is not enough — it would hide a
  double-claim paired with a miss. The disjointness of the two sets is the proof.

Note this exercises the *poller*, not just the claim query: the loop's
claim → commit → dispatch → mark cycle (model A) must not re-expose a row.
``packages/database`` separately pins the raw ``claim_outbox_batch`` function.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from radar_database import Database, OutboxEvent, write_outbox_event
from radar_outbox_worker.dispatcher import EventDispatcher, TargetResolver
from radar_outbox_worker.poller import Poller
from radar_outbox_worker.retry import DispatchProcessor
from sqlalchemy import func, select

#: Enough events that both pollers are racing for overlapping batches, not
#: politely taking turns on a queue too short to contend over.
EVENT_COUNT = 60

#: Keeps each poller busy long enough that the other is mid-claim at the same
#: time, so the claim transactions genuinely overlap.
HANDLER_DELAY_SECONDS = 0.01
POLL_INTERVAL_SECONDS = 0.01

DRAIN_TIMEOUT_SECONDS = 30.0


async def _seed(db: Database, count: int) -> set[UUID]:
    """Write ``count`` pending outbox events; return their event_ids."""
    seeded: set[UUID] = set()
    async with db.session() as session:
        for i in range(count):
            event = await write_outbox_event(
                session,
                event_type="alert.normalized",
                target_service="watcher-agent",
                payload={"i": i},
                correlation_id=uuid4(),
            )
            seeded.add(event.event_id)
        await session.commit()
    return seeded


async def _outbox_count(db: Database) -> int:
    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    return int(count or 0)


def _recording_handler(
    claimed: list[UUID], processor: DispatchProcessor
) -> Callable[[OutboxEvent], Awaitable[None]]:
    """Record what THIS poller claimed, then run the real dispatch/mark path.

    Appends to a list (not a set) so a poller double-handling one event inside
    itself would also be caught, not silently deduplicated.
    """

    async def handler(event: OutboxEvent) -> None:
        claimed.append(event.event_id)
        await asyncio.sleep(HANDLER_DELAY_SECONDS)
        await processor(event)

    return handler


async def test_two_pollers_never_double_process(db: Database) -> None:
    seeded = await _seed(db, EVENT_COUNT)
    assert len(seeded) == EVENT_COUNT

    # The real dispatch path; the target always accepts, so each delivered event
    # is removed from the outbox and the run drains.
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    processor = DispatchProcessor(
        db, EventDispatcher(client, TargetResolver(), SecretStr("t" * 64))
    )

    claimed_a: list[UUID] = []
    claimed_b: list[UUID] = []
    poller_a = Poller(
        db,
        _recording_handler(claimed_a, processor),
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )
    poller_b = Poller(
        db,
        _recording_handler(claimed_b, processor),
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
    )

    # Both loops run at the same time and race for the same rows.
    task_a = asyncio.create_task(poller_a.run())
    task_b = asyncio.create_task(poller_b.run())

    drained = False
    try:
        async with asyncio.timeout(DRAIN_TIMEOUT_SECONDS):
            while await _outbox_count(db) > 0:
                await asyncio.sleep(0.05)
        drained = True
    except TimeoutError:
        drained = False
    finally:
        poller_a.stop()
        poller_b.stop()
        await asyncio.gather(task_a, task_b)
        await client.aclose()

    assert drained, f"outbox did not drain within {DRAIN_TIMEOUT_SECONDS}s"

    set_a, set_b = set(claimed_a), set(claimed_b)

    # Neither poller handled the same event twice on its own.
    assert len(claimed_a) == len(set_a), "poller A handled an event more than once"
    assert len(claimed_b) == len(set_b), "poller B handled an event more than once"

    # THE PROOF: the two pollers' claimed sets are disjoint. No event was claimed
    # — and therefore dispatched — by both workers. This is exactly what
    # FOR UPDATE SKIP LOCKED buys, and it cannot be shown by a mock or SQLite.
    overlap = set_a & set_b
    assert set_a.isdisjoint(set_b), (
        f"{len(overlap)} event(s) were double-processed by both pollers: "
        f"{sorted(str(e) for e in overlap)[:5]}"
    )

    # And nothing was dropped: together they handled exactly the seeded set.
    # (Disjointness alone could still hide a miss; the union pins that.)
    assert set_a | set_b == seeded, "claimed events do not cover the seeded set"
    assert len(claimed_a) + len(claimed_b) == EVENT_COUNT

    # Both pollers genuinely participated — otherwise the disjointness assertion
    # above is vacuous (one idle poller trivially collides with nobody).
    assert set_a, "poller A claimed nothing — no real contention"
    assert set_b, "poller B claimed nothing — no real contention"
