"""Critical test 2: concurrent poller isolation.

Insert 10 outbox events, run two pollers simultaneously, and verify every event
is claimed by exactly one poller — no duplicates. This proves the poller's
``FOR UPDATE SKIP LOCKED`` query keeps concurrent workers on disjoint sets.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from radar_database import Database, claim_outbox_batch, write_outbox_event


async def test_two_pollers_never_claim_the_same_event(db: Database) -> None:
    correlation_id = uuid4()
    async with db.session() as session:
        for i in range(10):
            await write_outbox_event(
                session,
                event_type="alert.normalized",
                target_service="watcher-agent",
                payload={"i": i},
                correlation_id=correlation_id,
            )
        await session.commit()

    # Force both pollers to reach the claim at the same time, so they genuinely
    # race for the same rows.
    barrier = asyncio.Barrier(2)

    async def poll() -> list[UUID]:
        async with db.session() as session:
            await barrier.wait()
            claimed = await claim_outbox_batch(session, limit=10)
            ids = [event.id for event in claimed]
            await session.commit()
            return ids

    first, second = await asyncio.gather(poll(), poll())

    all_ids = first + second
    assert len(all_ids) == 10, "every event should be claimed exactly once"
    assert len(set(all_ids)) == 10, "no event was claimed by both pollers"
    assert set(first).isdisjoint(second), "pollers claimed disjoint sets"
