"""RADAR database layer.

Async SQLAlchemy 2.0 persistence for every RADAR service, on top of asyncpg:

- ``connection`` — the async engine and session factory.
- ``models`` — SQLAlchemy models mirroring every table in the schema.
- ``outbox`` — the transactional outbox writer and the
  ``FOR UPDATE SKIP LOCKED`` poller the outbox worker drives.
- ``repository`` — per-model repositories, including the ``processed_events``
  idempotency check that makes event handling safe to retry.

The transactional outbox is the backbone of agent communication: an event is
written in the same transaction as the state change that produced it, so a
committed state change and its event are all-or-nothing. See
docs/adr/0003-postgres-outbox.md.

Public symbols are re-exported here so consumers import from the top level::

    from radar_database import Base, get_sessionmaker, write_outbox_event
"""

from __future__ import annotations

from .connection import (
    Database,
    create_engine,
    create_session_factory,
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
from .outbox import (
    DEFAULT_BATCH_SIZE,
    MAX_ATTEMPTS,
    RETRY_DELAYS_SECONDS,
    STATUS_DEAD_LETTER,
    STATUS_PENDING,
    STATUS_PROCESSING,
    claim_outbox_batch,
    claim_stuck_processing,
    dead_letter_now,
    mark_dispatched,
    mark_failed,
    write_outbox_event,
)
from .repository import (
    AlertRepository,
    AuditLogRepository,
    FeedbackRepository,
    IncidentRepository,
    InvestigationPlanRepository,
    OutboxEventRepository,
    ProcessedEventRepository,
    RecommendationRepository,
    Repository,
    RunbookDocumentRepository,
    is_already_processed,
    mark_processed,
)

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # connection
    "Database",
    "create_engine",
    "create_session_factory",
    # models
    "Base",
    "Alert",
    "AuditLog",
    "Feedback",
    "Incident",
    "InvestigationPlan",
    "OutboxEvent",
    "ProcessedEvent",
    "Recommendation",
    "RunbookDocument",
    # outbox
    "write_outbox_event",
    "claim_outbox_batch",
    "claim_stuck_processing",
    "dead_letter_now",
    "mark_dispatched",
    "mark_failed",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "STATUS_DEAD_LETTER",
    "DEFAULT_BATCH_SIZE",
    "MAX_ATTEMPTS",
    "RETRY_DELAYS_SECONDS",
    # repository
    "Repository",
    "AlertRepository",
    "IncidentRepository",
    "InvestigationPlanRepository",
    "RecommendationRepository",
    "FeedbackRepository",
    "OutboxEventRepository",
    "ProcessedEventRepository",
    "RunbookDocumentRepository",
    "AuditLogRepository",
    "is_already_processed",
    "mark_processed",
]
