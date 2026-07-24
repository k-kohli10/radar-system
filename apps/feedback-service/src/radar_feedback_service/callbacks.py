"""Parse a NotificationInteraction into a validated callback — pure, load-bearing.

This is the second deep-treatment area of Phase 9. A mis-parse writes feedback
against the WRONG recommendation or resolves the WRONG incident, so the mapping
from Slack's echoed ``(action_id, value)`` to a RADAR ``(action, recommendation_id)``
is validated strictly and NEVER guesses:

- ``action_id`` must be one of the closed :class:`InteractionAction` set. An
  unrecognised action is REJECTED, not defaulted — defaulting an unknown click to,
  say, a resolve would act on an intent the user never expressed.
- ``value`` must be present and a well-formed UUID. A missing or malformed value is
  REJECTED, never passed through to a query as a raw string.

Both fields are data RADAR put on the button and Slack echoed back (a user cannot
alter a button's value), so this is origin-trusted input — but origin-trust
justifies skipping a *signature*, not skipping *validation*. The parse is the
validation.

There is deliberately NO incident id to parse: ``NotificationInteraction`` does not
carry one (by design), so the handler derives the incident from the recommendation
this parser identifies. "Resolves the wrong incident" is therefore not defended
against here — it is unrepresentable, because the only identity this layer produces
is the recommendation id and the incident follows from its row.

Rejection RAISES :class:`CallbackParseError` rather than returning a sentinel: the
reject must be visible (logged by the caller), never a silent no-op that looks like
a handled click.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from radar_common import RadarError
from radar_contracts import NotificationInteraction


class InteractionAction(StrEnum):
    """The closed set of actions a card button can carry.

    The ``str`` value is the ``action_id`` set on the button when the card is built
    (feedback-service's ``cards``) and echoed back on a click. One definition, shared
    by the formatter that writes it and this parser that reads it, so the two cannot
    disagree about what a button means — the same drift-guard as the fingerprint
    tuple. (Correction is deliberately absent: it is a modal, deferred to stage 5.)
    """

    FEEDBACK_UP = "feedback.up"
    FEEDBACK_DOWN = "feedback.down"
    RESOLVE = "incident.resolve"


class CallbackParseError(RadarError):
    """A callback whose action or value could not be understood. Rejected, loudly."""


@dataclass(frozen=True)
class ParsedCallback:
    """A validated card interaction: which action, on which recommendation.

    ``action`` and ``recommendation_id`` are the validated, load-bearing outputs.
    ``user_id`` / ``channel_id`` / ``message_ts`` are carried through unchanged for
    the handler to record the actor and update or reply to the card.
    """

    action: InteractionAction
    recommendation_id: UUID
    user_id: str
    channel_id: str
    message_ts: str


def parse_callback(interaction: NotificationInteraction) -> ParsedCallback:
    """Validate ``interaction`` into a :class:`ParsedCallback`, or raise.

    Raises :class:`CallbackParseError` on an ``action_id`` outside
    :class:`InteractionAction`, or a ``value`` that is absent or not a UUID. Never
    guesses a default for either — an un-understood click does nothing rather than
    the wrong thing.
    """
    try:
        action = InteractionAction(interaction.action_id)
    except ValueError as exc:
        raise CallbackParseError(
            f"unknown interaction action_id: {interaction.action_id!r}"
        ) from exc

    if interaction.value is None:
        raise CallbackParseError(
            "interaction carries no value; expected a recommendation id"
        )
    try:
        recommendation_id = UUID(interaction.value)
    except ValueError as exc:
        raise CallbackParseError(
            f"interaction value is not a recommendation id: {interaction.value!r}"
        ) from exc

    return ParsedCallback(
        action=action,
        recommendation_id=recommendation_id,
        user_id=interaction.user_id,
        channel_id=interaction.channel_id,
        message_ts=interaction.message_ts,
    )
