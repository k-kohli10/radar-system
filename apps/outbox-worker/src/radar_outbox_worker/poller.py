"""The outbox poller: claim pending events and drive them to a handler.

This is the heart of the worker. It runs the **claim → commit → dispatch → mark**
model (model A):

1. In one short transaction, :func:`~radar_database.claim_outbox_batch` flips up
   to ``batch_size`` due ``pending`` rows to ``processing`` using
   ``FOR UPDATE SKIP LOCKED`` and ``RETURNING`` them, then that transaction
   **commits immediately** — releasing the row locks and leaving the rows visibly
   ``processing``. Because the claim subquery skips rows another worker has
   locked, two workers polling the same table claim *disjoint* sets: no event is
   ever handled twice. That is the load-bearing property of this phase.
2. The (now unlocked) claimed events are dispatched **outside** any transaction,
   one at a time, each with its own hard timeout. Holding no DB locks across the
   HTTP call is the whole reason the claim commits first.
3. Each event's outcome is recorded in a *fresh* transaction by the injected
   handler (``mark_dispatched`` on success, ``mark_failed`` — backoff or
   dead-letter — on failure). The handler is wired in over later commits; the
   poller only knows "hand me an event, I'll get it to a terminal state."

Crash safety: if the process dies between the claim commit and the mark, the
event is stranded in ``processing`` (the pending-only claim query never sees it
again). The **reaper** — added with the retry/dead-letter commit — recovers such
rows by funnelling them through the same ``mark_failed`` promotion path (which
increments ``attempts``, so even a poison event that crashes mid-dispatch
eventually reaches ``dead_letter`` instead of looping forever). The poller is
built so that recovery loop slots in alongside this one.

See the outbox worker specification in the implementation plan and
docs/adr/0003-postgres-outbox.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from radar_common import bind_correlation_id, clear_context, get_logger
from radar_database import (
    DEFAULT_BATCH_SIZE,
    STATUS_PENDING,
    STATUS_PROCESSING,
    Database,
    OutboxEvent,
    claim_outbox_batch,
)
from radar_telemetry import OutboxMetrics
from sqlalchemy import func, select

#: A claimed event is handed to this callable, which owns its own transaction for
#: dispatch + marking. Kept injectable so the poller stays decoupled from the
#: dispatcher (built next) and is trivial to drive from the concurrency test.
EventHandler = Callable[[OutboxEvent], Awaitable[None]]

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
"""Idle sleep when a poll returns no due events.

A fixed short interval keeps per-hop pipeline latency low without busy-spinning.
If per-hop latency ever needs to be tighter than one poll, the future option is
Postgres ``LISTEN``/``NOTIFY`` (ingestion signals on outbox insert) — not needed
now, and it would not change the claim query that provides the safety guarantee.
"""

log = get_logger("outbox-worker.poller")


class Poller:
    """Runs the claim-and-drive loop until asked to stop.

    Sequential per-event handling is deliberate: correctness (no double dispatch)
    comes from ``SKIP LOCKED`` across workers, not from intra-worker concurrency,
    and one-at-a-time processing bounds the graceful-shutdown drain to a single
    in-flight dispatch rather than a whole batch.
    """

    def __init__(
        self,
        database: Database,
        handle_event: EventHandler,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        metrics: OutboxMetrics | None = None,
    ) -> None:
        self._database = database
        self._handle_event = handle_event
        self._batch_size = batch_size
        self._poll_interval = poll_interval_seconds
        self._metrics = metrics
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the loop to stop after the current in-flight event.

        Idempotent and thread/callback-safe (SIGTERM wiring lands with the
        graceful-shutdown commit). The loop finishes the event it is dispatching,
        abandons any un-started events in the current batch (the reaper recovers
        those), and returns.
        """
        self._stop.set()

    async def run(self) -> None:
        """Poll, claim, and drive events until :meth:`stop` is signalled."""
        log.info(
            "outbox.poller.started",
            batch_size=self._batch_size,
            poll_interval_seconds=self._poll_interval,
        )
        while not self._stop.is_set():
            await self._sample_gauges()
            try:
                events = await self._claim_batch()
            except Exception as exc:
                # A transient DB failure must not kill the worker: log the class
                # (never a message — it may embed the DSN), back off, retry.
                log.error("outbox.poll.failed", error_type=type(exc).__name__)
                await self._sleep_or_stop(self._poll_interval)
                continue
            if not events:
                await self._sleep_or_stop(self._poll_interval)
                continue
            await self._handle_batch(events)
        log.info("outbox.poller.stopped")

    async def _claim_batch(self) -> Sequence[OutboxEvent]:
        """Claim one batch in its own committed transaction (model A).

        The transaction commits *before* returning, so no row locks are held
        across the subsequent dispatches.
        """
        async with self._database.session() as session:
            events = await claim_outbox_batch(session, limit=self._batch_size)
            await session.commit()
        if events:
            log.info("outbox.batch.claimed", count=len(events))
        return events

    async def _handle_batch(self, events: Sequence[OutboxEvent]) -> None:
        for index, event in enumerate(events):
            if self._stop.is_set():
                # Draining: leave the un-started tail in 'processing' for the
                # reaper rather than run the whole batch past the shutdown budget.
                log.info(
                    "outbox.batch.drain_interrupted", remaining=len(events) - index
                )
                return
            bind_correlation_id(event.correlation_id)
            try:
                await self._handle_event(event)
            except Exception as exc:
                # Defence in depth: the handler is expected to catch dispatch
                # failures itself and record them via mark_failed. If it raises
                # anyway, the event stays 'processing' and the reaper recovers it,
                # so a single bad event can never take down the loop.
                log.error(
                    "outbox.event.handler_failed",
                    event_id=str(event.event_id),
                    error_type=type(exc).__name__,
                )
            finally:
                clear_context()

    async def _sample_gauges(self) -> None:
        """Sample outbox depth once per loop (decision: pending + processing).

        One grouped count over the partial index. Metrics failures are isolated —
        a gauge sample must never break the loop or mask a real poll error.
        """
        if self._metrics is None:
            return
        try:
            stmt = (
                select(OutboxEvent.status, func.count())
                .where(OutboxEvent.status.in_([STATUS_PENDING, STATUS_PROCESSING]))
                .group_by(OutboxEvent.status)
            )
            async with self._database.session() as session:
                rows = (await session.execute(stmt)).all()
            counts = {status: n for status, n in rows}
            self._metrics.depth.set(counts.get(STATUS_PENDING, 0))
            self._metrics.processing.set(counts.get(STATUS_PROCESSING, 0))
        except Exception as exc:
            log.error("outbox.gauge_sample_failed", error_type=type(exc).__name__)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for ``seconds``, waking early if :meth:`stop` is signalled.

        So shutdown never waits out a full idle poll interval.
        """
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass  # normal path: the interval elapsed without a stop signal
