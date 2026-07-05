"""Feedback contracts.

``FeedbackEvent`` mirrors the ``feedback`` table: an engineer's reaction to a
delivered recommendation, captured from the Slack RCA card buttons
(helpful / not helpful / correction). It records which provider and model alias
produced the recommendation being rated, for downstream quality analysis.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class FeedbackEvent(BaseModel):
    """Feedback on a recommendation, captured from a Slack RCA card."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    recommendation_id: UUID = Field(description="Recommendation being rated.")
    incident_id: UUID = Field(description="Incident the recommendation belongs to.")
    correlation_id: UUID = Field(
        description="Trace-wide correlation id, inherited from the incident.",
    )
    sentiment: str = Field(
        max_length=16,
        description="Reaction, e.g. 'helpful', 'not_helpful', 'correction'.",
    )
    correction_text: str | None = Field(
        default=None,
        description="Free-text correction supplied by the engineer, if any.",
    )
    slack_user_id: str | None = Field(
        default=None,
        max_length=64,
        description="Slack user id who gave the feedback.",
    )
    slack_message_ts: str | None = Field(
        default=None,
        max_length=64,
        description="Slack message timestamp of the RCA card.",
    )
    llm_provider: str = Field(
        max_length=64,
        description="Provider that produced the rated recommendation.",
    )
    model_alias: str = Field(
        max_length=64,
        description="Gateway mode/alias of the rated recommendation.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
