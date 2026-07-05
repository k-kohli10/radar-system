"""Transactional outbox and idempotency contracts.

``OutboxEvent`` mirrors the ``outbox_events`` table: agents write these in the
same transaction as their state changes, and the outbox worker dispatches them
over HTTP. ``ProcessedEvent`` mirrors the ``processed_events`` idempotency
table: each service records the ``event_id`` values it has already handled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class OutboxEvent(BaseModel):
    """A durable, at-least-once event awaiting dispatch.

    Written transactionally alongside the state change that produced it, then
    polled and delivered by the outbox worker to ``target_service``'s
    ``POST /events`` endpoint. ``event_id`` is the logical identity carried in
    the delivery payload and recorded by consumers for idempotency; ``id`` is
    the database row key.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4, description="Database row key.")
    event_id: UUID = Field(
        default_factory=uuid4,
        description="Logical event identity; unique, used for idempotency.",
    )
    event_type: str = Field(
        max_length=128,
        description="Event name, e.g. 'alert.normalized', 'recommendation.created'.",
    )
    target_service: str = Field(
        max_length=64,
        description="Service to dispatch to, e.g. 'watcher-agent'.",
    )
    payload: dict[str, Any] = Field(description="Event body delivered to the target.")
    correlation_id: UUID = Field(
        description="Trace-wide correlation id threaded through the pipeline.",
    )
    status: str = Field(
        default="pending",
        max_length=32,
        description="Dispatch status: 'pending', 'processing', or 'dead_letter'.",
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description="Number of dispatch attempts made so far.",
    )
    last_error: str | None = Field(
        default=None,
        description="Error from the most recent failed dispatch, if any.",
    )
    process_after: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Earliest time the event may be dispatched (retry backoff).",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProcessedEvent(BaseModel):
    """An idempotency marker: one event handled by one service.

    Inserted in the same transaction as an event's state changes. A row's
    presence means the ``(event_id, processed_by)`` pair was already handled, so
    redelivery is a no-op. Mirrors the ``processed_events`` table.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(description="Logical event id that was processed.")
    processed_by: str = Field(
        max_length=64,
        description="Service that processed the event, e.g. 'planner-agent'.",
    )
    processed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the event was processed.",
    )
