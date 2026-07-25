"""Socket Mode receive side: payload translation and the ack-then-dispatch listener.

Two things get proven here, no real socket:

- :func:`to_interaction` maps a Slack ``block_actions`` payload to the neutral
  ``NotificationInteraction`` — the vendor boundary. It forwards a well-formed
  button click and drops anything else (wrong type, no actions, missing identity
  fields) rather than hand the app a half-empty event.
- The listener acks the envelope FIRST — inside Slack's redelivery window — then
  dispatches. The ack ordering is the load-bearing bit: processing after the ack is
  the deliberate at-most-once trade for an interactive control.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from radar_contracts import BotMention, NotificationInteraction
from radar_plugin_notifications_slack import (
    ack_and_dispatch,
    to_interaction,
    to_mention,
)
from slack_sdk.socket_mode.request import SocketModeRequest


def _block_actions(
    *,
    action_id: str = "feedback.up",
    value: str | None = "rec-123",
    user: str | None = "U1",
    channel: str | None = "C1",
    ts: str | None = "1720000000.0001",
) -> dict[str, Any]:
    """A Slack block_actions payload, with fields omittable for the drop tests."""
    action: dict[str, Any] = {"action_id": action_id, "block_id": "b1"}
    if value is not None:
        action["value"] = value
    payload: dict[str, Any] = {"type": "block_actions", "actions": [action]}
    if user is not None:
        payload["user"] = {"id": user}
    if channel is not None:
        payload["channel"] = {"id": channel}
    if ts is not None:
        payload["message"] = {"ts": ts}
    return payload


def _app_mention(
    *,
    event_type: str = "app_mention",
    text: str | None = "<@U0BOT> status",
    user: str | None = "U1",
    channel: str | None = "C1",
    ts: str | None = "1720000000.0001",
) -> dict[str, Any]:
    """A Slack events-API app_mention payload, with fields omittable for the drop
    tests. The events envelope wraps the event under ``event``."""
    event: dict[str, Any] = {"type": event_type}
    if text is not None:
        event["text"] = text
    if user is not None:
        event["user"] = user
    if channel is not None:
        event["channel"] = channel
    if ts is not None:
        event["ts"] = ts
    return {"type": "event_callback", "event": event}


# --- to_interaction: the vendor boundary -----------------------------------------


def test_translates_a_button_click() -> None:
    interaction = to_interaction(_block_actions())
    assert interaction == NotificationInteraction(
        action_id="feedback.up",
        value="rec-123",
        user_id="U1",
        channel_id="C1",
        message_ts="1720000000.0001",
    )


def test_value_passed_through_when_absent() -> None:
    """A control with no value yields value=None — the app decides if it's required."""
    interaction = to_interaction(_block_actions(value=None))
    assert interaction is not None
    assert interaction.value is None


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"type": "view_submission"}, "not a block_actions (a modal submit)"),
        ({"type": "block_actions", "actions": []}, "no actions"),
        (_block_actions(user=None), "missing user"),
        (_block_actions(channel=None), "missing channel"),
        (_block_actions(ts=None), "missing message ts"),
        (_block_actions(action_id=""), "empty action id"),
    ],
)
def test_drops_anything_not_a_complete_button_click(
    payload: dict[str, Any], why: str
) -> None:
    """Incomplete or non-button payloads translate to None, never a partial event."""
    assert to_interaction(payload) is None, why


# --- ack_and_dispatch: ack first, then dispatch ----------------------------------
#
# Driven directly with a fake client, so no SocketModeClient is constructed (which
# would leave an unclosed aiohttp session). SlackInteractionSource is the thin
# wiring that registers this on a real socket; the logic under test is here.


async def test_acks_then_dispatches_translated_interaction() -> None:
    received: list[NotificationInteraction] = []

    async def handler(interaction: NotificationInteraction) -> None:
        received.append(interaction)

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="interactive", envelope_id="env-1", payload=_block_actions()
    )

    await ack_and_dispatch(fake_client, request, handler)

    # Acked with the envelope id, exactly once.
    fake_client.send_socket_mode_response.assert_awaited_once()
    ack = fake_client.send_socket_mode_response.call_args.args[0]
    assert ack.envelope_id == "env-1"
    # And the handler got the translated interaction.
    assert len(received) == 1
    assert received[0].action_id == "feedback.up"
    assert received[0].value == "rec-123"


async def test_ack_precedes_dispatch() -> None:
    """The ack must be sent BEFORE the handler runs — the redelivery-window trade.

    Records the order of the two awaits; a handler that ran before the ack would
    risk the ~3s window elapsing and Slack redelivering while work is in flight.
    """
    order: list[str] = []

    async def handler(interaction: NotificationInteraction) -> None:
        order.append("dispatch")

    fake_client = AsyncMock()
    fake_client.send_socket_mode_response.side_effect = lambda *a, **k: order.append(
        "ack"
    )
    request = SocketModeRequest(
        type="interactive", envelope_id="env-1", payload=_block_actions()
    )

    await ack_and_dispatch(fake_client, request, handler)
    assert order == ["ack", "dispatch"]


async def test_acks_but_does_not_dispatch_non_interactive() -> None:
    """A non-interactive envelope (e.g. an events_api delivery) is still acked, so
    Slack does not redeliver it, but the interaction handler is not called."""
    received: list[NotificationInteraction] = []

    async def handler(interaction: NotificationInteraction) -> None:
        received.append(interaction)

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="events_api", envelope_id="env-2", payload={"type": "event_callback"}
    )

    await ack_and_dispatch(fake_client, request, handler)

    fake_client.send_socket_mode_response.assert_awaited_once()
    assert received == []


async def test_acks_but_does_not_dispatch_malformed_interaction() -> None:
    """An interactive envelope that doesn't translate (incomplete payload) is acked
    but not dispatched — the drop happens after the ack, never a redelivery loop."""
    received: list[NotificationInteraction] = []

    async def handler(interaction: NotificationInteraction) -> None:
        received.append(interaction)

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="interactive",
        envelope_id="env-3",
        payload=_block_actions(user=None),  # missing identity → to_interaction None
    )

    await ack_and_dispatch(fake_client, request, handler)

    fake_client.send_socket_mode_response.assert_awaited_once()
    assert received == []


# --- to_mention: the vendor boundary for @radar mentions -------------------------


def test_translates_an_app_mention() -> None:
    mention = to_mention(_app_mention())
    assert mention == BotMention(
        text="<@U0BOT> status",
        user_id="U1",
        channel_id="C1",
        message_ts="1720000000.0001",
    )


def test_mention_text_keeps_the_raw_bot_handle() -> None:
    """The mention is handed over with the bot handle still in the text — stripping it
    is the app's parser's job, not the transport's, so to_mention must not touch it."""
    mention = to_mention(_app_mention(text="<@U0BOT> last 5 for order-service"))
    assert mention is not None
    assert mention.text == "<@U0BOT> last 5 for order-service"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        ({"event": {"type": "message"}}, "not an app_mention"),
        ({"type": "event_callback"}, "no event"),
        (_app_mention(text=None), "missing text"),
        (_app_mention(user=None), "missing user"),
        (_app_mention(channel=None), "missing channel"),
        (_app_mention(ts=None), "missing message ts"),
    ],
)
def test_drops_anything_not_a_complete_app_mention(
    payload: dict[str, Any], why: str
) -> None:
    """Incomplete or non-mention events translate to None, never a partial mention."""
    assert to_mention(payload) is None, why


# --- ack_and_dispatch: mentions route to the mention handler ---------------------


async def test_acks_then_dispatches_a_mention() -> None:
    mentions: list[BotMention] = []

    async def interaction_handler(interaction: NotificationInteraction) -> None:
        raise AssertionError("interaction handler must not see a mention")

    async def mention_handler(mention: BotMention) -> None:
        mentions.append(mention)

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="events_api", envelope_id="env-m", payload=_app_mention()
    )

    await ack_and_dispatch(fake_client, request, interaction_handler, mention_handler)

    fake_client.send_socket_mode_response.assert_awaited_once()
    assert len(mentions) == 1
    assert mentions[0].text == "<@U0BOT> status"


async def test_ack_precedes_mention_dispatch() -> None:
    """The ack must be sent BEFORE the mention handler runs — the same 3s-window trade
    as interactions. A mention that acks late (after slow handler work) gets
    redelivered by Slack and processed twice; the ack-first ordering is what prevents
    that, and it must hold on the mention path, not just the interaction one.
    """
    order: list[str] = []

    async def interaction_handler(interaction: NotificationInteraction) -> None:
        raise AssertionError("interaction handler must not see a mention")

    async def mention_handler(mention: BotMention) -> None:
        order.append("dispatch")

    fake_client = AsyncMock()
    fake_client.send_socket_mode_response.side_effect = lambda *a, **k: order.append(
        "ack"
    )
    request = SocketModeRequest(
        type="events_api", envelope_id="env-mo", payload=_app_mention()
    )

    await ack_and_dispatch(fake_client, request, interaction_handler, mention_handler)
    assert order == ["ack", "dispatch"]


async def test_mention_acked_but_dropped_without_a_mention_handler() -> None:
    """The pre-bot state: with no mention_handler registered, an app_mention is still
    acked (no redelivery loop) but goes nowhere — the interaction side runs alone."""

    async def interaction_handler(interaction: NotificationInteraction) -> None:
        raise AssertionError("interaction handler must not see a mention")

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="events_api", envelope_id="env-m2", payload=_app_mention()
    )

    await ack_and_dispatch(fake_client, request, interaction_handler)

    fake_client.send_socket_mode_response.assert_awaited_once()


async def test_interactive_routes_to_interaction_not_mention() -> None:
    """With both handlers registered, an interactive envelope goes to the interaction
    handler and never the mention handler — the routing is by envelope type."""
    interactions: list[NotificationInteraction] = []

    async def interaction_handler(interaction: NotificationInteraction) -> None:
        interactions.append(interaction)

    async def mention_handler(mention: BotMention) -> None:
        raise AssertionError("mention handler must not see an interaction")

    fake_client = AsyncMock()
    request = SocketModeRequest(
        type="interactive", envelope_id="env-i", payload=_block_actions()
    )

    await ack_and_dispatch(fake_client, request, interaction_handler, mention_handler)

    assert len(interactions) == 1
