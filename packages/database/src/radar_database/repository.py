"""Repository layer.

A generic async :class:`Repository` (get / add / list) with a thin, typed
subclass per model. Repositories are intentionally minimal here — the shared
package gives every model a consistent data-access surface; service-specific
queries are added per phase as the pipeline needs them.

The one piece of domain logic that lives here is idempotency:
:func:`is_already_processed` and :func:`mark_processed` back the
``processed_events`` table. Every agent checks ``is_already_processed`` before
handling an event and writes the marker in the same transaction as its state
changes, so redelivery of an at-least-once event is a safe no-op. See the
idempotency pattern in the implementation plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from radar_common import NotFoundError, utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .lifecycle import (
    STATUS_CLOSED,
    STATUS_INVESTIGATING,
    STATUS_OPEN,
    STATUS_RESOLVED,
    InvalidStateTransitionError,
    is_valid_transition,
    transition_audit_event_type,
)
from .models import (
    Alert,
    AuditLog,
    Base,
    Feedback,
    Incident,
    InvestigationPlan,
    OutboxEvent,
    ProcessedEvent,
    Recommendation,
    RunbookDocument,
)
from .outbox import STATUS_PENDING

#: The incidents an on-call engineer means by "open": still live, not yet resolved.
#: ``investigating`` (a human has been told, ADR 0016 Amendment 1) is unresolved work,
#: so the bot's ``open``/``status`` count it alongside ``open`` — the actionable set,
#: not the literal ``status = 'open'`` row state. Resolved and closed are excluded.
#: This is the SAME non-terminal set as ingestion's ``find_resolvable_incident``
#: (``_RESOLVABLE_INCIDENT_STATES``, ADR 0016 Amendment 2); "live" has one meaning
#: across the codebase. The two can't share a constant across the package boundary, so
#: keep them in step by hand if the lifecycle ever grows a state.
_ACTIVE_STATUSES = (STATUS_OPEN, STATUS_INVESTIGATING)


class Repository[ModelT: Base]:
    """Generic async repository providing get / add / list for one model."""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    async def get(self, pk: UUID) -> ModelT | None:
        """Return the row with primary key ``pk``, or ``None``."""
        return await self._session.get(self._model, pk)

    async def add(self, instance: ModelT) -> ModelT:
        """Add ``instance`` and flush (no commit); returns it with defaults set."""
        self._session.add(instance)
        await self._session.flush()
        return instance

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        """Return a page of rows."""
        stmt = select(self._model).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return result.scalars().all()


class AlertRepository(Repository[Alert]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Alert)


class IncidentRepository(Repository[Incident]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Incident)

    async def transition_status(
        self,
        incident_id: UUID,
        new_status: str,
        *,
        actor: str,
        correlation_id: UUID | None = None,
        audit_payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> Incident:
        """Move an incident to ``new_status``, validated, with an audit row.

        The single enforced entry point for incident status changes (ADR 0016). It:

        1. loads the incident under ``SELECT ... FOR UPDATE`` — the row lock that
           makes the read-modify-write safe against a concurrent transition;
        2. rejects an illegal edge by raising :class:`InvalidStateTransitionError`,
           having written NOTHING (see below);
        3. on a legal edge, sets the status, stamps ``updated_at``, and stamps
           ``resolved_at`` / ``closed_at`` when entering those states, then writes
           one ``audit_log`` row — all on the caller's session.

        **Atomicity is the contract.** The status change and its audit row are
        added to the caller's transaction and this method does NOT commit: the
        caller owns the boundary, exactly as ``persist_alert`` and
        ``mark_processed`` do. So a valid transition and its audit row commit
        together or roll back together — a rolled-back transition leaves neither a
        status change nor an audit row, which is what makes the audit log
        trustworthy: no row ever claims a transition that did not happen.

        **The rejection path writes nothing.** An illegal transition raises and
        adds no row. Recording the rejected ATTEMPT is forensic and belongs to the
        caller, in its own transaction — adding an ``incident.invalid_transition``
        row here and then raising would lose it to the caller's rollback. The
        caller catches :class:`InvalidStateTransitionError` and logs the attempt with
        ``INVALID_TRANSITION_AUDIT_EVENT``.

        ``correlation_id`` defaults to the incident's own; ``occurred_at`` (the
        moment the transition happened — e.g. an Alertmanager webhook's time)
        defaults to now and stamps ``updated_at`` plus any lifecycle timestamp.

        Raises :class:`~radar_common.NotFoundError` if the incident does not exist
        — ingestion owns incident creation, so a transition targeting a missing
        incident is a bug upstream, not a row to invent here.
        """
        # FOR UPDATE, not SKIP LOCKED: a concurrent transition on the same incident
        # must BLOCK and then re-read the status the winner wrote, not skip the row.
        incident = (
            await self._session.execute(
                select(Incident).where(Incident.id == incident_id).with_for_update()
            )
        ).scalar_one_or_none()
        if incident is None:
            raise NotFoundError(f"incident {incident_id} does not exist")

        current = incident.status
        if not is_valid_transition(current, new_status):
            raise InvalidStateTransitionError(
                from_status=current,
                attempted_status=new_status,
                incident_id=incident_id,
            )

        when = occurred_at if occurred_at is not None else utcnow()
        incident.status = new_status
        incident.updated_at = when
        if new_status == STATUS_RESOLVED:
            incident.resolved_at = when
        if new_status == STATUS_CLOSED:
            # Reserved in Phase 9: no caller reaches `closed` yet (@radar close is
            # deferred), so in practice closed_at stays NULL. The branch exists so
            # the state machine is whole, not half-built, for when that caller lands.
            incident.closed_at = when

        self._session.add(
            AuditLog(
                event_type=transition_audit_event_type(new_status),
                entity_type="incident",
                entity_id=incident_id,
                correlation_id=correlation_id
                if correlation_id is not None
                else incident.correlation_id,
                actor=actor,
                payload=audit_payload if audit_payload is not None else {},
            )
        )
        await self._session.flush()
        return incident

    # --- bot read queries (SELECT only; no method here mutates state) -------------

    async def count_active(self) -> int:
        """How many incidents are live (open or investigating) — the status count."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.status.in_(_ACTIVE_STATUSES))
        )
        return int(count or 0)

    async def list_active(self, *, limit: int) -> Sequence[Incident]:
        """The live incidents, newest first, capped at ``limit`` — the ``open`` list.

        ``limit`` is the caller's cap (the bot's ``bot_max_rows``); the ``LIMIT`` keeps
        a busy incident feed from dumping every open row into Slack.
        """
        result = await self._session.execute(
            select(Incident)
            .where(Incident.status.in_(_ACTIVE_STATUSES))
            .order_by(Incident.opened_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def recent(
        self, *, limit: int, service: str | None = None
    ) -> Sequence[Incident]:
        """The most recent incidents, newest first, capped — the ``last <n>`` list.

        Optionally filtered to one ``service``. ``limit`` bounds the result the same way
        :meth:`list_active` does: the caller passes the clamped ``last <n>``.
        """
        stmt = select(Incident)
        if service is not None:
            stmt = stmt.where(Incident.service_name == service)
        stmt = stmt.order_by(Incident.opened_at.desc()).limit(limit)
        return (await self._session.execute(stmt)).scalars().all()

    async def count_opened_between(self, start: datetime, end: datetime) -> int:
        """Incidents opened in the half-open window ``[start, end)`` (``summary``)."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.opened_at >= start, Incident.opened_at < end)
        )
        return int(count or 0)

    async def count_resolved_between(self, start: datetime, end: datetime) -> int:
        """Incidents resolved in ``[start, end)`` — the other half of ``summary``."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.resolved_at >= start, Incident.resolved_at < end)
        )
        return int(count or 0)


class InvestigationPlanRepository(Repository[InvestigationPlan]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, InvestigationPlan)


class RecommendationRepository(Repository[Recommendation]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Recommendation)

    async def latest_for_incident(self, incident_id: UUID) -> Recommendation | None:
        """The RCA for an incident — for the ``incident <id>`` detail view.

        At most one exists (``idx_rec_one_per_incident``); ``ORDER BY ... LIMIT 1`` is
        belt-and-braces so this never depends on that invariant to return a single row.
        """
        return (
            await self._session.execute(
                select(Recommendation)
                .where(Recommendation.incident_id == incident_id)
                .order_by(Recommendation.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def latest_created_at(self) -> datetime | None:
        """When the most recent RCA was produced — the "last RCA" line of ``status``.
        ``None`` when no recommendation exists yet."""
        return await self._session.scalar(select(func.max(Recommendation.created_at)))


class FeedbackRepository(Repository[Feedback]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Feedback)

    async def count_by_sentiment_between(
        self, start: datetime, end: datetime
    ) -> dict[str, int]:
        """Feedback rows in ``[start, end)`` grouped by sentiment — for ``summary``.

        A sentiment with no rows is simply absent from the mapping; the caller reads a
        missing key as zero.
        """
        result = await self._session.execute(
            select(Feedback.sentiment, func.count())
            .where(Feedback.created_at >= start, Feedback.created_at < end)
            .group_by(Feedback.sentiment)
        )
        return {sentiment: int(count) for sentiment, count in result.all()}


class OutboxEventRepository(Repository[OutboxEvent]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, OutboxEvent)

    async def count_pending(self) -> int:
        """Undispatched outbox events — the "outbox depth" line of ``status``.
        A growing depth means dispatch is falling behind."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == STATUS_PENDING)
        )
        return int(count or 0)


class RunbookDocumentRepository(Repository[RunbookDocument]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, RunbookDocument)


class AuditLogRepository(Repository[AuditLog]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, AuditLog)


async def is_already_processed(
    session: AsyncSession, event_id: UUID, service_name: str
) -> bool:
    """Return whether ``event_id`` has already been handled by ``service_name``."""
    result = await session.execute(
        select(ProcessedEvent).where(
            ProcessedEvent.event_id == event_id,
            ProcessedEvent.processed_by == service_name,
        )
    )
    return result.scalar_one_or_none() is not None


async def mark_processed(
    session: AsyncSession, event_id: UUID, service_name: str
) -> ProcessedEvent:
    """Record ``event_id`` as handled by ``service_name`` (no commit).

    Written in the same transaction as the handler's state changes. ``event_id``
    is the table's primary key, so a concurrent duplicate delivery that races
    past :func:`is_already_processed` raises ``IntegrityError`` on flush — the
    guard that keeps at-least-once processing exactly-once in effect.
    """
    marker = ProcessedEvent(event_id=event_id, processed_by=service_name)
    session.add(marker)
    await session.flush()
    return marker


class ProcessedEventRepository(Repository[ProcessedEvent]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, ProcessedEvent)

    async def is_already_processed(self, event_id: UUID, service_name: str) -> bool:
        return await is_already_processed(self._session, event_id, service_name)

    async def mark_processed(self, event_id: UUID, service_name: str) -> ProcessedEvent:
        return await mark_processed(self._session, event_id, service_name)
