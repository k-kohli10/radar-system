"""Critical test 3: processed-event idempotency.

Mark an event processed for a service, then verify ``is_already_processed``
returns True on redelivery and a handler skips the second processing.
"""

from __future__ import annotations

from uuid import uuid4

from radar_database import Database, is_already_processed, mark_processed


async def test_processed_event_is_idempotent(db: Database) -> None:
    event_id = uuid4()
    service = "planner-agent"

    async with db.session() as session:
        assert await is_already_processed(session, event_id, service) is False
        await mark_processed(session, event_id, service)
        await session.commit()

    # Redelivery of the same event to the same service is recognised as handled.
    async with db.session() as session:
        assert await is_already_processed(session, event_id, service) is True

    # A different service has not processed this event.
    async with db.session() as session:
        assert await is_already_processed(session, event_id, "reasoner-agent") is False


async def test_handler_skips_second_processing(db: Database) -> None:
    event_id = uuid4()
    service = "planner-agent"
    processed = 0

    async def handle() -> None:
        nonlocal processed
        async with db.session() as session:
            if await is_already_processed(session, event_id, service):
                return  # already handled — skip
            processed += 1
            await mark_processed(session, event_id, service)
            await session.commit()

    await handle()
    await handle()  # redelivery

    assert processed == 1, "the event must be processed exactly once"
