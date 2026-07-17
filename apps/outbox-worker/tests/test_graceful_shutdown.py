"""Graceful shutdown: finish the in-flight dispatch, claim no more, never hang.

The contract is SIGTERM -> stop polling -> wait at most 30s for in-flight work ->
exit. uvicorn turns SIGTERM into a lifespan shutdown; what *we* own — and what
these tests pin — is what that shutdown does:

1. **In-flight work is finished, not abandoned.** A stop signal arriving while an
   event is mid-dispatch must not kill it. The poller waits for it.
2. **No new work is claimed** once stopping. Otherwise a draining pod keeps
   pulling events it has no time to deliver.
3. **The unstarted tail stays recoverable.** Events already claimed but not yet
   started are left in ``processing`` — not deleted, not lost — for the reaper.
4. **The drain is bounded.** Work that overruns the budget is cancelled so the
   pod exits, rather than hanging past its termination grace period.

Asserting only "run() returns after stop()" would prove none of these — it would
pass just as happily on a poller that dropped in-flight work on the floor. The
tests below assert on the states that would *hide* those failures: that the task
is still running while work is in flight, and the exact pending/processing/
delivered split of the seeded events afterwards.

Note on the boundary: turning SIGTERM into a lifespan shutdown is uvicorn's job,
not ours, so it is not re-tested here. Everything downstream of that — the drain
itself — is.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry
from radar_database import (
    STATUS_PENDING,
    STATUS_PROCESSING,
    Database,
    OutboxEvent,
    mark_dispatched,
    write_outbox_event,
)
from radar_outbox_worker import main as main_module
from radar_outbox_worker.poller import Poller
from sqlalchemy import func, select
from tokens import token_for

BATCH_SIZE = 10  # the poller's default claim size
SEEDED = 20  # two batches' worth: the second must never be claimed


async def _seed(db: Database, count: int) -> None:
    async with db.session() as session:
        for i in range(count):
            await write_outbox_event(
                session,
                event_type="alert.normalized",
                target_service="watcher-agent",
                payload={"i": i},
                correlation_id=uuid4(),
            )
        await session.commit()


async def _count(db: Database, status: str) -> int:
    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == status)
        )
    return int(count or 0)


async def _total(db: Database) -> int:
    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(OutboxEvent))
    return int(count or 0)


async def test_stop_finishes_the_in_flight_event_and_claims_no_more(
    db: Database,
) -> None:
    """A stop mid-dispatch drains that event, claims nothing new, loses nothing."""
    await _seed(db, SEEDED)

    in_flight = asyncio.Event()  # set once an event is actually mid-dispatch
    release = asyncio.Event()  # the test decides when that dispatch completes
    handled: list[UUID] = []

    async def handler(event: OutboxEvent) -> None:
        handled.append(event.event_id)
        in_flight.set()
        await release.wait()  # hold the worker inside the dispatch
        async with db.session() as session:  # then deliver for real
            row = await session.get(OutboxEvent, event.id)
            assert row is not None
            await mark_dispatched(session, row)
            await session.commit()

    poller = Poller(db, handler, poll_interval_seconds=0.01)
    task = asyncio.create_task(poller.run())

    # Wait until one event is genuinely in flight, then "SIGTERM".
    await asyncio.wait_for(in_flight.wait(), timeout=5.0)
    poller.stop()

    # The poller must still be draining — it may NOT abandon the in-flight event.
    # (A poller that dropped in-flight work would have exited by now.)
    await asyncio.sleep(0.2)
    assert not task.done(), "stop() must not abandon the in-flight dispatch"

    release.set()
    await asyncio.wait_for(task, timeout=10.0)  # drains, then exits

    # 1. Exactly the in-flight event was handled, and it completed: delivered.
    assert len(handled) == 1, "no further events may be started after stop()"

    # 2. No new batch was claimed after the stop: the second batch of 10 is
    #    untouched and still pending, ready for another worker.
    assert await _count(db, STATUS_PENDING) == SEEDED - BATCH_SIZE == 10

    # 3. The claimed-but-unstarted tail of the first batch is left in
    #    'processing' — recoverable by the reaper, not deleted and not lost.
    assert await _count(db, STATUS_PROCESSING) == BATCH_SIZE - 1 == 9

    # 4. Nothing vanished: 20 seeded == 1 delivered (row removed) + 19 still held.
    assert await _total(db) == SEEDED - 1 == 19


async def test_drain_is_bounded_and_cannot_hang_past_the_budget(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Work that overruns the drain budget is cancelled, so the pod still exits.

    The dispatcher's own 10s hard timeout normally keeps in-flight work well
    inside the 30s budget, so this is the safety net: if a dispatch somehow hangs
    anyway, shutdown must not wait for it forever. Driven through the real
    lifespan with the budget shrunk, against a handler that never returns.
    """
    (tmp_path / "postgres_dsn").write_text(database_url)
    (tmp_path / "agent_token").write_text("a" * 64)
    # The worker's own token (above) is not a dispatch token: it authenticates
    # callers of /admin/*, and cannot authenticate the worker TO a target. Without
    # this map startup fails and /readyz stays 503 — which is the intended
    # behaviour, but it would leave this test with nothing to drain.
    (tmp_path / "dispatch_tokens").write_text(
        f"watcher-agent: {token_for('watcher-agent')}\n"
    )
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))

    started = asyncio.Event()

    class HangingProcessor:
        """Stands in for a dispatch that never comes back."""

        def __init__(self, database: object, dispatcher: object, **kwargs: object):
            pass

        async def __call__(self, event: OutboxEvent) -> None:
            started.set()
            await asyncio.sleep(3600)  # never returns on its own

    monkeypatch.setattr(main_module, "DispatchProcessor", HangingProcessor)
    monkeypatch.setattr(main_module, "DRAIN_TIMEOUT_SECONDS", 0.3)

    await _seed(db, 1)
    app = main_module.create_app(
        metrics_registry=CollectorRegistry(), with_tracing=False
    )

    async with app.router.lifespan_context(app):
        # The worker is now wedged inside a dispatch that will never finish.
        await asyncio.wait_for(started.wait(), timeout=5.0)
        shutdown_began = time.monotonic()
    drain_seconds = time.monotonic() - shutdown_began

    # Bounded by the budget (plus cancellation), not by the hanging work.
    assert drain_seconds < 5.0, (
        f"drain took {drain_seconds:.1f}s — it waited on hung work instead of "
        "cancelling at the budget"
    )

    # The wedged event is left in 'processing' for the reaper: not lost.
    assert await _count(db, STATUS_PROCESSING) == 1
