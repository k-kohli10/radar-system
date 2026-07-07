"""Transactional outbox writer and poller.

The outbox is how RADAR services communicate: instead of calling each other over
HTTP directly, a service writes an ``outbox_events`` row in the *same
transaction* as the state change that produced it (:func:`write_outbox_event`),
so the state change and its event are all-or-nothing. The outbox worker then
:func:`claim_outbox_batch` es pending events with ``FOR UPDATE SKIP LOCKED`` —
guaranteeing no two workers ever claim the same event — dispatches them, and
records the result with :func:`mark_dispatched` or :func:`mark_failed`.

See docs/adr/0003-postgres-outbox.md and the outbox worker specification in the
implementation plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from radar_common import new_event_id, utcnow
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, OutboxEvent

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DEAD_LETTER = "dead_letter"

DEFAULT_BATCH_SIZE = 10

#: A dispatch is retried with growing backoff; after MAX_ATTEMPTS failed attempts
#: the event is dead-lettered. Keys are the attempt number about to be scheduled.
RETRY_DELAYS_SECONDS: dict[int, int] = {2: 5, 3: 15, 4: 60, 5: 300}
MAX_ATTEMPTS = 5


async def write_outbox_event(
    session: AsyncSession,
    *,
    event_type: str,
    target_service: str,
    payload: dict[str, Any],
    correlation_id: UUID,
    event_id: UUID | None = None,
) -> OutboxEvent:
    """Add a pending outbox event to ``session`` (does not commit).

    Call inside the same transaction as the state change that produced the
    event; the caller commits both together. ``event_id`` defaults to a fresh
    UUID and is the logical identity consumers record for idempotency.
    """
    event = OutboxEvent(
        event_id=event_id if event_id is not None else new_event_id(),
        event_type=event_type,
        target_service=target_service,
        payload=payload,
        correlation_id=correlation_id,
    )
    session.add(event)
    await session.flush()
    return event


async def claim_outbox_batch(
    session: AsyncSession, *, limit: int = DEFAULT_BATCH_SIZE
) -> Sequence[OutboxEvent]:
    """Atomically claim up to ``limit`` pending, due events for this worker.

    A single ``UPDATE ... RETURNING`` whose subquery selects the oldest due
    events ``FOR UPDATE SKIP LOCKED`` and flips them to ``processing``. Because
    the subquery skips rows another transaction has locked, concurrent workers
    claim disjoint sets — no event is ever handled twice. Must run inside a
    transaction; the row locks are held until the caller commits.
    """
    due = (
        select(OutboxEvent.id)
        .where(
            OutboxEvent.status == STATUS_PENDING,
            OutboxEvent.process_after <= func.now(),
        )
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(due))
        .values(status=STATUS_PROCESSING, updated_at=func.now())
        .returning(OutboxEvent)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def mark_dispatched(session: AsyncSession, event: OutboxEvent) -> None:
    """Remove a successfully dispatched event from the outbox."""
    await session.delete(event)


async def mark_failed(
    session: AsyncSession,
    event: OutboxEvent,
    *,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Record a failed dispatch, then reschedule or dead-letter the event.

    Increments the attempt count and stores ``error``. Under ``MAX_ATTEMPTS``
    failures the event is set back to ``pending`` with backoff; on the final
    failure it is set to ``dead_letter`` and an ``audit_log`` row is written in
    the same transaction. Returns ``True`` if the event was dead-lettered (so
    the caller can emit the dead-letter metric).
    """
    moment = now if now is not None else utcnow()
    event.attempts += 1
    event.last_error = error
    event.updated_at = moment

    if event.attempts >= MAX_ATTEMPTS:
        event.status = STATUS_DEAD_LETTER
        session.add(
            AuditLog(
                event_type="outbox.dead_letter",
                entity_type="outbox_event",
                entity_id=event.id,
                correlation_id=event.correlation_id,
                payload={
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "target_service": event.target_service,
                    "attempts": event.attempts,
                    "last_error": error,
                },
            )
        )
        return True

    delay = RETRY_DELAYS_SECONDS[event.attempts + 1]
    event.status = STATUS_PENDING
    event.process_after = moment + timedelta(seconds=delay)
    return False
