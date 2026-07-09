"""Alert contracts.

``NormalizedAlert`` is the vendor-neutral alert produced by the ingestion
normalizer after a Prometheus, Kibana, or mock alert is received. It is the
payload carried by the ``alert.normalized`` outbox event to the watcher agent.

The fields mirror the ``alerts`` table in the RADAR data model, except for
``incident_id``, which is assigned later by the watcher when the alert is
correlated onto an incident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    """Canonical alert/incident severity.

    A closed vocabulary, declared most-severe to least-severe, so a given
    severity is the *same* value regardless of source and thus compares equal
    downstream (watcher escalation, and the dedup fingerprint that embeds
    severity). Ingestion validates each source's reported severity against this
    set and rejects anything else with 422 — sources are configured to emit
    these values; ingestion never maps or translates one spelling to another.

    Equality only. Do not rely on ``str`` ordering (``"critical" < "high"`` is
    lexical, not severity order); rank-based comparison for escalation is the
    watcher's concern and derives from the declaration order here.
    """

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class NormalizedAlert(BaseModel):
    """A single alert, normalized to a vendor-neutral shape.

    Produced by ingestion regardless of source (Prometheus alertmanager,
    Kibana Watcher, or the mock endpoint). ``raw_payload`` retains the original
    source body verbatim for audit and debugging; all other fields are the
    normalized projection the pipeline reasons over.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(
        default_factory=uuid4,
        description="Application-side generated identifier for this alert.",
    )
    source: str = Field(
        max_length=64,
        description="Originating detector, e.g. 'prometheus', 'kibana', 'mock'.",
    )
    source_alert_id: str | None = Field(
        default=None,
        max_length=256,
        description="Identifier assigned by the source system, if any.",
    )
    fingerprint: str = Field(
        max_length=64,
        description="Correlation fingerprint: sha256(service:alert:severity).",
    )
    service_name: str = Field(
        max_length=128,
        description="Name of the affected service, e.g. 'order-service'.",
    )
    alert_name: str = Field(
        max_length=256,
        description="Name of the alerting rule, e.g. 'OrderProcessingFailureRate'.",
    )
    severity: Severity = Field(
        description="Canonical severity, validated against the Severity set.",
    )
    status: str = Field(
        default="firing",
        max_length=32,
        description="Alert lifecycle status, e.g. 'firing' or 'resolved'.",
    )
    raw_payload: dict[str, Any] = Field(
        description="Original source payload, retained verbatim.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized key/value labels from the source.",
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="Normalized human-readable annotations from the source.",
    )
    fired_at: datetime = Field(
        description="When the source detector fired this alert.",
    )
    resolved_at: datetime | None = Field(
        default=None,
        description="When the alert resolved, if it has.",
    )
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When RADAR ingestion received this alert.",
    )
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Trace-wide correlation id threaded through the pipeline.",
    )
