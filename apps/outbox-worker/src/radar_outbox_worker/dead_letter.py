"""The reaper: recover events stranded in ``processing`` by a crashed worker.

Model A commits the claim *before* dispatching, so a worker that dies
mid-dispatch leaves its claimed rows in ``processing`` — which the pending-only
:func:`~radar_database.claim_outbox_batch` never re-selects. Without recovery
those events are stranded forever. The reaper is a separate periodic task that
sweeps them back into the pipeline.

Each sweep, :func:`~radar_database.claim_stuck_processing` locks rows whose
``updated_at`` is older than the interval (a server-side age threshold,
``FOR UPDATE SKIP LOCKED`` so concurrent reapers recover disjoint sets), and each
is re-driven through :func:`~radar_database.mark_failed` with ``immediate=True``.
That is the *same* promotion path normal retries use: it counts the attempt (so a
poison event that crashes the worker mid-dispatch still reaches ``dead_letter``
instead of looping forever) and, below the ceiling, re-pends with
``process_after = NOW()`` — a crash is not the target's fault, so retry at once.
There is no second dead-letter path; the reaper never decides promotion itself.

The sweep interval and the stuck-age threshold are deliberately the same value
(``interval_seconds``): sweep every N seconds for rows stuck more than N seconds —
one operational knob, no magic number. Each sweep is one short-lived transaction
(open, claim, re-drive, commit, close); no session is held across the idle sleep.

Shutdown: :meth:`stop` ends the loop cleanly, and because a sweep either commits
whole or rolls back, a stop mid-sweep leaves rows in ``processing`` for the next
run — never half-reaped. The graceful-shutdown commit drives ``stop`` on SIGTERM
alongside the poll loop, so both drain together within the budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from radar_common import bind_correlation_id, clear_context, get_logger
from radar_database import (
    DEFAULT_BATCH_SIZE,
    STATUS_DEAD_LETTER,
    STATUS_PENDING,
    AuditLog,
    Database,
    OutboxEvent,
    claim_stuck_processing,
    mark_failed,
)
from radar_telemetry import OutboxMetrics
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_REAPER_INTERVAL_SECONDS = 60

#: Stored in ``last_error`` on a reaped event, so an exhausted dead-letter whose
#: last failure was a crash is distinguishable from a dispatch failure.
REAP_ERROR = "reaped: worker crashed with the event in 'processing'"

log = get_logger("outbox-worker.reaper")


class Reaper:
    """Periodically re-drives events stranded in ``processing`` by a crashed worker.

    Mirrors :class:`~radar_outbox_worker.poller.Poller`'s lifecycle (``run`` /
    ``stop`` / interruptible sleep); the shared "periodic task with graceful stop"
    shape is worth extracting once a third consumer appears.
    """

    def __init__(
        self,
        database: Database,
        *,
        interval_seconds: int = DEFAULT_REAPER_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        metrics: OutboxMetrics | None = None,
    ) -> None:
        self._database = database
        self._interval = interval_seconds
        self._batch_size = batch_size
        self._metrics = metrics
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the reaper to stop after the current sweep (SIGTERM-safe)."""
        self._stop.set()

    async def run(self) -> None:
        """Sweep stuck events every ``interval_seconds`` until :meth:`stop`."""
        log.info("outbox.reaper.started", interval_seconds=self._interval)
        while not self._stop.is_set():
            try:
                await self._sweep()
            except Exception as exc:
                # A transient DB failure must not kill the reaper: log the class
                # (never a message — it may embed the DSN), back off, retry.
                log.error("outbox.reaper.sweep_failed", error_type=type(exc).__name__)
            await self._sleep_or_stop(self._interval)
        log.info("outbox.reaper.stopped")

    async def _sweep(self) -> None:
        async with self._database.session() as session:
            stuck = await claim_stuck_processing(
                session, older_than_seconds=self._interval, limit=self._batch_size
            )
            for event in stuck:
                bind_correlation_id(event.correlation_id)
                try:
                    dead = await mark_failed(
                        session, event, error=REAP_ERROR, immediate=True
                    )
                    if dead:
                        if self._metrics is not None:
                            self._metrics.dead_letter_total.inc()
                        log.error(
                            "outbox.reaper.dead_lettered",
                            event_id=str(event.event_id),
                            attempts=event.attempts,
                        )
                    else:
                        log.warning(
                            "outbox.reaper.recovered",
                            event_id=str(event.event_id),
                            attempts=event.attempts,
                        )
                finally:
                    clear_context()
            await session.commit()
        if stuck:
            log.info("outbox.reaper.swept", count=len(stuck))

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep for ``seconds``, waking early if :meth:`stop` is signalled."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass  # normal path: the interval elapsed without a stop signal


async def list_dead_letters(
    session: AsyncSession, *, limit: int, offset: int
) -> Sequence[OutboxEvent]:
    """Return a page of dead-lettered events, most-recently-dead-lettered first."""
    stmt = (
        select(OutboxEvent)
        .where(OutboxEvent.status == STATUS_DEAD_LETTER)
        .order_by(OutboxEvent.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def requeue_dead_letter(
    session: AsyncSession, event_id: UUID
) -> OutboxEvent | None:
    """Requeue the dead-lettered event with ``event_id`` for another attempt.

    Resets it to ``pending`` with ``attempts=0`` and ``process_after=NOW()`` so
    the poller claims it immediately, keeping ``last_error`` for forensics, and
    writes an ``outbox.requeued`` ``audit_log`` row snapshotting the prior
    attempts and error. Returns the row, or ``None`` if no ``dead_letter`` event
    has that ``event_id`` (the caller answers 404). Does not commit — the caller
    controls the transaction. ``event_id`` is the stable business key operators
    see in the dead-letter list and audit trail, not the internal row id.
    """
    result = await session.execute(
        select(OutboxEvent).where(
            OutboxEvent.event_id == event_id,
            OutboxEvent.status == STATUS_DEAD_LETTER,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        return None

    now = await session.scalar(select(func.now()))
    assert isinstance(now, datetime)  # NOW() always returns one timestamptz row
    # Audit the prior state before resetting it.
    session.add(
        AuditLog(
            event_type="outbox.requeued",
            entity_type="outbox_event",
            entity_id=event.id,
            correlation_id=event.correlation_id,
            actor="admin",
            payload={
                "event_id": str(event.event_id),
                "previous_attempts": event.attempts,
                "previous_last_error": event.last_error,
            },
        )
    )
    event.status = STATUS_PENDING
    event.attempts = 0
    event.process_after = now
    event.updated_at = now
    return event
