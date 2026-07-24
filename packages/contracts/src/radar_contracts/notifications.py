"""Notification backend contract.

``NotificationBackend`` is the vendor-neutral interface for delivering
notifications (RADAR uses it to send Slack RCA cards and threaded bot replies).
It is a ``typing.Protocol`` (structural), never an ABC, and references no vendor
type. The Slack implementation lives in ``plugins/notifications/slack/`` and
imports ``slack-sdk`` there; nothing in this module does.

``NotificationInteraction`` is the reverse direction: a user acting on a delivered
notification (a button on the RCA card). The Slack plugin produces it from a
``block_actions`` payload received over Socket Mode and feedback-service consumes
it. Like ``BotCommand``, it carries only opaque strings — no Slack types cross
this boundary.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


@runtime_checkable
class NotificationBackend(Protocol):
    """Interface for a notification delivery backend.

    Implementations translate the generic call into their own transport (for
    RADAR, the Slack Web API). ``blocks`` carries optional backend-specific rich
    content, such as an interactive RCA card; backends without rich support fall
    back to ``text``.
    """

    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        """Deliver a notification and return a backend message reference.

        The returned reference identifies the delivered message (for RADAR, the
        Slack message timestamp) so replies can be threaded and feedback linked
        back to it. ``thread_ref`` posts this message as a reply under an
        existing message reference.
        """
        ...


class NotificationInteraction(BaseModel):
    """A user action on a delivered notification — a button click on the RCA card.

    Vendor-neutral. The Slack plugin builds this from a ``block_actions`` payload
    it receives over Socket Mode and hands it to feedback-service, which alone
    interprets it. Every field is an opaque string; no Slack SDK type reaches the
    app.

    ``action_id`` names WHICH control was activated — RADAR's own action id, set on
    the button when the card is built and echoed back on the click. ``value`` is
    that control's payload: RADAR round-trips the *recommendation id* here and
    nothing else. In particular there is NO incident id on this contract, by
    design — the consumer derives the incident from the recommendation the value
    names, so a callback can never reference a mismatched (recommendation, incident)
    pair. Both fields are data the app POSTED and Slack echoes back, not free input
    a user can alter; the consumer still validates them (parse, closed action set,
    row lookup) before acting.

    ``channel_id`` and ``message_ts`` locate the card so the consumer can update it
    in place (``chat.update``) or reply to the acting user; ``user_id`` is who acted.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(
        max_length=128,
        description="RADAR action id of the activated control (e.g. the button).",
    )
    value: str | None = Field(
        default=None,
        max_length=256,
        description="The control's value; RADAR round-trips the recommendation id.",
    )
    user_id: str = Field(max_length=64, description="Id of the user who acted.")
    channel_id: str = Field(
        max_length=64, description="Channel the notification was delivered on."
    )
    message_ts: str = Field(
        max_length=64,
        description="Timestamp of the notification message, for update/reply.",
    )
