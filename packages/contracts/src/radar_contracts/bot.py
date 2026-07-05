"""Slack bot contracts.

``BotCommand`` is a parsed ``@radar`` chat command; ``BotResponse`` is the
formatted reply the bot posts back in-thread. These are plain Pydantic models
with no vendor types: Slack identifiers are carried as opaque strings, and the
Slack SDK lives only in the feedback-service, never here.

Supported v1 commands::

    @radar status
    @radar open
    @radar incident <id>
    @radar last <n> [for <service>]
    @radar summary [today|yesterday]
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BotCommandType(StrEnum):
    """The closed set of v1 bot command verbs."""

    STATUS = "status"
    OPEN = "open"
    INCIDENT = "incident"
    LAST = "last"
    SUMMARY = "summary"


class BotCommand(BaseModel):
    """A parsed ``@radar`` mention.

    ``command`` is the verb; the remaining fields are the arguments relevant to
    that verb (unused arguments stay ``None``). The ``slack_*``/``channel``/
    ``thread_ref`` fields carry the routing context needed to reply in-thread.
    """

    model_config = ConfigDict(extra="forbid")

    command: BotCommandType = Field(description="Parsed command verb.")
    raw_text: str = Field(description="Original mention text, minus the bot handle.")
    incident_id: str | None = Field(
        default=None,
        description="Incident id argument for 'incident <id>'.",
    )
    count: int | None = Field(
        default=None,
        ge=1,
        description="Row count argument for 'last <n>'.",
    )
    service: str | None = Field(
        default=None,
        description="Service filter for 'last <n> for <service>'.",
    )
    period: str | None = Field(
        default=None,
        description="Period for 'summary', e.g. 'today' or 'yesterday'.",
    )
    slack_user_id: str | None = Field(
        default=None,
        description="Slack user id that issued the command.",
    )
    channel: str | None = Field(
        default=None,
        description="Channel the mention arrived on.",
    )
    thread_ref: str | None = Field(
        default=None,
        description="Message reference to reply under, for in-thread replies.",
    )


class BotResponse(BaseModel):
    """A formatted bot reply.

    Bot replies are posted ephemerally in the same thread as the mention.
    ``blocks`` carries optional backend-specific rich content; backends without
    rich support fall back to ``text``.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Formatted response text.")
    blocks: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional backend-specific rich content.",
    )
    ephemeral: bool = Field(
        default=True,
        description="Whether the reply is visible only to the requesting user.",
    )
