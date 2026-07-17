"""Incident, investigation plan, and recommendation contracts.

These three models mirror the ``incidents``, ``investigation_plans``, and
``recommendations`` tables in the RADAR data model. Together they carry an
incident from correlation (watcher) through planning (planner) to a delivered
root-cause recommendation (reasoner).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from .alerts import Severity


class Confidence(StrEnum):
    """Confidence level of a recommendation.

    The closed set defined by the reasoner system prompt schema
    (``"confidence": "low|medium|high"``).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PlanStep(BaseModel):
    """One ordered step in an investigation plan.

    Matches the shape emitted by the planner's YAML templates and echoed in the
    reasoner context bundle's ``investigation_steps``.
    """

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, description="1-based position of this step.")
    description: str = Field(description="What the engineer should investigate.")


class RecommendedAction(BaseModel):
    """One ordered action in a recommendation.

    Matches the reasoner output schema: ``{"order": 1, "action": "..."}``.
    """

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1, description="1-based position of this action.")
    #: Non-empty, and that is load-bearing rather than tidy. The reasoner validates a
    #: language model's output against this contract, and a model that emits
    #: ``{"order": 1, "action": ""}`` would otherwise produce a recommendation telling
    #: an engineer to do nothing, in a card that looks perfectly well-formed. An empty
    #: action is not an action.
    action: str = Field(
        min_length=1, description="A specific, actionable remediation step."
    )


class Incident(BaseModel):
    """A correlated incident grouping one or more alerts.

    Created by the watcher when an alert does not match an open incident within
    the correlation window. Mirrors the ``incidents`` table.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Trace-wide correlation id; unique per incident.",
    )
    fingerprint: str = Field(
        max_length=64,
        description="Correlation fingerprint shared by grouped alerts.",
    )
    service_name: str = Field(
        max_length=128,
        description="Name of the primary affected service.",
    )
    title: str = Field(
        max_length=512,
        description="Human-readable incident title.",
    )
    severity: Severity = Field(
        description="Canonical incident severity, possibly escalated from the alert.",
    )
    status: str = Field(
        default="open",
        max_length=32,
        description="Lifecycle status, e.g. 'open', 'resolved', 'closed'.",
    )
    alert_count: int = Field(
        default=1,
        ge=1,
        description="Number of alerts correlated onto this incident.",
    )
    opened_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When the incident was opened.",
    )
    resolved_at: datetime | None = Field(
        default=None,
        description="When the underlying condition resolved, if it has.",
    )
    closed_at: datetime | None = Field(
        default=None,
        description="When the incident was administratively closed, if it has.",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Last time this incident row was updated.",
    )


class InvestigationPlan(BaseModel):
    """An ordered investigation plan for an incident.

    Produced by the planner from YAML templates keyed by
    ``service_name:alert_name`` (falling back to ``_default``). Mirrors the
    ``investigation_plans`` table; there is exactly one plan per incident.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID = Field(description="Incident this plan belongs to.")
    correlation_id: UUID = Field(
        description="Trace-wide correlation id, inherited from the incident.",
    )
    steps: list[PlanStep] = Field(description="Ordered investigation steps.")
    template_key: str | None = Field(
        default=None,
        max_length=128,
        description="Template key used, e.g. 'service:AlertName', or None for default.",
    )
    status: str = Field(
        default="pending",
        max_length=32,
        description="Plan lifecycle status, e.g. 'pending'.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Recommendation(BaseModel):
    """A root-cause recommendation for an incident.

    Produced by the reasoner from an LLM completion, or from the template
    fallback when the LLM gateway is unavailable (``is_fallback=True``). Mirrors
    the ``recommendations`` table.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    incident_id: UUID = Field(description="Incident this recommendation is for.")
    plan_id: UUID = Field(description="Investigation plan this was reasoned from.")
    correlation_id: UUID = Field(
        description="Trace-wide correlation id, inherited from the incident.",
    )
    llm_provider: str = Field(
        max_length=64,
        description="Provider that produced the recommendation, e.g. 'openai'.",
    )
    model_alias: str = Field(
        max_length=64,
        description="Gateway mode/alias used, e.g. 'extended'.",
    )
    model_id: str = Field(
        max_length=128,
        description="Concrete model id, e.g. 'gpt-4o'.",
    )
    root_cause: str = Field(description="The assessed likely root cause.")
    confidence: Confidence = Field(description="Confidence in the assessment.")
    recommended_actions: list[RecommendedAction] = Field(
        description="Ordered remediation actions.",
    )
    context_bundle: dict[str, Any] = Field(
        default_factory=dict,
        description="Context assembled for the LLM (incident metadata, runbooks).",
    )
    raw_llm_response: str | None = Field(
        default=None,
        description="Raw model response text, retained for audit. Never logged.",
    )
    is_fallback: bool = Field(
        default=False,
        description="True when generated by template fallback (LLM unavailable).",
    )
    prompt_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Prompt tokens consumed, if reported.",
    )
    completion_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Completion tokens produced, if reported.",
    )
    latency_ms: int | None = Field(
        default=None,
        ge=0,
        description="End-to-end LLM latency in milliseconds, if measured.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
