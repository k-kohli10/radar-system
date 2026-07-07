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
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


class InvestigationPlanRepository(Repository[InvestigationPlan]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, InvestigationPlan)


class RecommendationRepository(Repository[Recommendation]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Recommendation)


class FeedbackRepository(Repository[Feedback]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, Feedback)


class OutboxEventRepository(Repository[OutboxEvent]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, OutboxEvent)


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
