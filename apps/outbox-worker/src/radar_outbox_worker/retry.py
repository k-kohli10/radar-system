"""Record what a dispatch did: remove on success, reschedule with backoff on failure.

The poller claims and commits a batch (model A), then hands each event here.
:class:`DispatchProcessor` is the poller's event handler: it dispatches the event
and then, in a *fresh* transaction, records the outcome —

- **delivered** → :func:`~radar_database.mark_dispatched` removes the row;
- **permanent failure** (4xx that will never succeed on retry) →
  :func:`~radar_database.dead_letter_now` dead-letters it immediately, regardless
  of attempts, with an ``audit_log`` record;
- **transient failure** → :func:`~radar_database.mark_failed` reschedules it with
  growing backoff (``process_after`` off the server clock), and promotes it to
  ``dead_letter`` once the final attempt is spent.

The retryable-vs-permanent split comes from the dispatcher's classification,
which mirrors the gateway policy (timeouts/connection/429/5xx retry;
400/401/403/422 and other non-2xx are permanent).

Because the claim transaction already committed, the claimed ``OutboxEvent`` is
**detached**. We re-fetch it by id inside the recording transaction so the DELETE
/ UPDATE act on a live, session-bound row carrying the current attempt count —
this worker exclusively owns the row (it is ``processing``, which the pending-only
claim query never re-selects), so the re-fetch races no one.

If recording raises (e.g. the database is briefly unreachable), the exception
propagates to the poller, which logs it and leaves the row in ``processing`` for
the reaper to recover — never a lost or double-delivered event.
"""

from __future__ import annotations

from radar_common import get_logger
from radar_database import (
    Database,
    OutboxEvent,
    dead_letter_now,
    mark_dispatched,
    mark_failed,
)

from radar_outbox_worker.dispatcher import EventDispatcher

log = get_logger("outbox-worker.retry")


class DispatchProcessor:
    """Dispatch one claimed event and record the outcome. The poller's handler."""

    def __init__(self, database: Database, dispatcher: EventDispatcher) -> None:
        self._database = database
        self._dispatcher = dispatcher

    async def __call__(self, event: OutboxEvent) -> None:
        result = await self._dispatcher.dispatch(event)
        async with self._database.session() as session:
            row = await session.get(OutboxEvent, event.id)
            if row is None:
                # We own the claimed ('processing') row; a missing row means it
                # was deleted out from under us. Nothing safe to do but note it.
                log.warning("outbox.record.row_missing", event_id=str(event.event_id))
                return
            if result.delivered:
                await mark_dispatched(session, row)
                log.info("outbox.record.delivered", event_id=str(event.event_id))
            elif result.permanent:
                await dead_letter_now(session, row, error=result.detail)
                log.error(
                    "outbox.dispatch.dead_lettered",
                    event_id=str(event.event_id),
                    reason=result.reason,
                    status_code=result.status_code,
                )
            else:
                dead_lettered = await mark_failed(session, row, error=result.detail)
                if dead_lettered:
                    log.error(
                        "outbox.retry.exhausted",
                        event_id=str(event.event_id),
                        attempts=row.attempts,
                        reason=result.reason,
                    )
                else:
                    log.warning(
                        "outbox.retry.scheduled",
                        event_id=str(event.event_id),
                        attempts=row.attempts,
                        reason=result.reason,
                        process_after=row.process_after.isoformat(),
                    )
            await session.commit()
