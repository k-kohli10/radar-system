"""Receive side: interactive callbacks AND ``@radar`` mentions over Socket Mode.

The send side (:mod:`backend`) posts and updates cards; this is the reverse. Two
kinds of thing arrive over the ONE Socket Mode connection this app opens:

- a user clicking a button on an RCA card — a Slack ``block_actions`` interaction,
  handed over as a vendor-neutral :class:`~radar_contracts.NotificationInteraction`;
- a user mentioning the bot (``@radar status``) — a Slack ``app_mention`` event,
  handed over as a vendor-neutral :class:`~radar_contracts.BotMention`.

Both are normalised here and given to feedback-service, which alone interprets them.
This module is pure transport: it translates Slack's payloads to the neutral contracts
and does NOT parse commands — stripping the bot handle and matching verbs is the app's
job, so the mention text is handed over exactly as Slack sent it.

**Socket Mode, and what it does (and does not) authenticate.** The source opens an
OUTBOUND WebSocket to Slack authenticated by the app-level token (``xapp-``). Slack
delivers only THIS app's own interactions and events over that connection — the
authenticated socket IS the authentication. There is deliberately no request-signature
check here: signing (``X-Slack-Signature``) is the HTTP Events API's mechanism, and the
property Socket Mode gives for free — only Slack can deliver here — is exactly what a
public HTTP endpoint would lose. When Phase 12 moves to Events API + ingress, signing
becomes mandatory; until then it does not apply.

The translations (:func:`to_interaction`, :func:`to_mention`) are pure; the socket
wiring around them is the thin shell. The class name predates mentions and is kept to
avoid churning the app's import in this slice; a rename can ride the wire-up commit.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from radar_contracts import BotMention, NotificationInteraction
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

InteractionHandler = Callable[[NotificationInteraction], Awaitable[None]]
"""What the app registers: handles one interaction. May take as long as it needs —
the envelope is already acked by the time it runs (see below)."""

MentionHandler = Callable[[BotMention], Awaitable[None]]
"""What the app registers for ``@radar`` mentions: handles one mention. Like the
interaction handler, it runs after the envelope is acked, so it may take its time."""

_INTERACTIVE = "interactive"
_BLOCK_ACTIONS = "block_actions"
_EVENTS_API = "events_api"
_APP_MENTION = "app_mention"


def to_interaction(payload: dict[str, Any]) -> NotificationInteraction | None:
    """Translate a Slack ``block_actions`` payload to a ``NotificationInteraction``.

    Returns ``None`` for anything that is not a button click carrying every
    identifying field. The plugin forwards only well-formed interactions and drops
    the rest, rather than handing the app a half-empty one to defend against — the
    neutral contract is always complete or absent.

    Only the FIRST action is taken: RADAR's card buttons each dispatch a single
    action, so a payload always carries exactly one. ``value`` is passed through as
    given (``None`` if the control had none); it is the app's job, not the plugin's,
    to decide whether a value is required and to validate it.
    """
    if payload.get("type") != _BLOCK_ACTIONS:
        return None
    actions = payload.get("actions") or []
    if not actions:
        return None
    action = actions[0]
    action_id = action.get("action_id")
    user_id = (payload.get("user") or {}).get("id")
    channel_id = (payload.get("channel") or {}).get("id")
    message_ts = (payload.get("message") or {}).get("ts")
    if not (action_id and user_id and channel_id and message_ts):
        # A block_actions missing its identity fields (user/channel/message) cannot
        # be acted on or replied to — drop it rather than forward an unusable event.
        logger.warning("slack interaction dropped: incomplete block_actions payload")
        return None
    return NotificationInteraction(
        action_id=action_id,
        value=action.get("value"),
        user_id=user_id,
        channel_id=channel_id,
        message_ts=message_ts,
    )


def to_mention(payload: dict[str, Any]) -> BotMention | None:
    """Translate a Slack ``app_mention`` events-API payload to a ``BotMention``.

    Returns ``None`` for anything that is not an ``app_mention`` carrying every
    identifying field — the plugin forwards only complete mentions and drops the rest,
    the same rule :func:`to_interaction` follows. The events-API envelope wraps the
    event under ``event``; only that inner ``app_mention`` is a bot command surface.

    ``text`` is passed through AS SLACK SENT IT — it still carries the leading
    ``<@BOT>`` handle. Stripping and parsing are the app's job (the next slice), not the
    transport's, so this hands over the raw text and nothing more. ``ts`` (the mention
    message's timestamp) becomes the thread the reply is posted under.
    """
    event = payload.get("event") or {}
    if event.get("type") != _APP_MENTION:
        return None
    text = event.get("text")
    user_id = event.get("user")
    channel_id = event.get("channel")
    message_ts = event.get("ts")
    if not (text and user_id and channel_id and message_ts):
        # A mention missing its identity/routing fields cannot be replied to — drop it
        # rather than forward an unusable event, as with block_actions above.
        logger.warning("slack mention dropped: incomplete app_mention payload")
        return None
    return BotMention(
        text=text,
        user_id=user_id,
        channel_id=channel_id,
        message_ts=message_ts,
    )


async def ack_and_dispatch(
    client: Any,
    request: SocketModeRequest,
    interaction_handler: InteractionHandler,
    mention_handler: MentionHandler | None = None,
) -> None:
    """Ack the envelope, then dispatch it to the handler for its kind.

    The whole logic of the receive side, separated from the vendor client wiring so
    it is testable with a fake ``client`` and no Socket Mode connection. Routes by
    envelope type: an ``interactive`` (button click) to ``interaction_handler``, an
    ``events_api`` ``app_mention`` to ``mention_handler``. Anything else — or a mention
    when no ``mention_handler`` is registered — is acked and dropped.

    The ack goes FIRST, before any work, inside Slack's ~3s window — an unacked
    envelope is redelivered. Processing therefore happens after the ack, so a
    handler that fails does NOT get the envelope redelivered: a lost 👍/👎 is a lost
    signal (harmless), a lost Resolve is recoverable (click again, and the
    alert-resolved webhook resolves it anyway), and a lost ``@radar`` query is a
    re-ask. Prompt ack over guaranteed processing is the deliberate trade for an
    interactive surface; see the phase notes on interaction idempotency.
    """
    await client.send_socket_mode_response(
        SocketModeResponse(envelope_id=request.envelope_id)
    )
    if request.type == _INTERACTIVE:
        interaction = to_interaction(request.payload)
        if interaction is not None:
            await interaction_handler(interaction)
    elif request.type == _EVENTS_API and mention_handler is not None:
        mention = to_mention(request.payload)
        if mention is not None:
            await mention_handler(mention)


class SlackInteractionSource:
    """A Socket Mode connection delivering button clicks and ``@radar`` mentions.

    Constructing this opens NO network connection; :meth:`start` connects and
    :meth:`close` disconnects, so the app owns the lifecycle in its lifespan. The
    ack/translate/dispatch logic lives in :func:`ack_and_dispatch`; this class is
    the thin wiring that registers it as a Socket Mode listener.

    ``mention_handler`` is optional: without it, ``app_mention`` events are acked and
    dropped (the state before the bot exists), so a deployment can wire the interaction
    side alone. The wire-up commit passes the mention handler to turn ``@radar`` on.
    """

    def __init__(
        self,
        *,
        app_token: str,
        bot_token: str,
        handler: InteractionHandler,
        mention_handler: MentionHandler | None = None,
    ) -> None:
        """Bind to Slack. ``app_token`` (``xapp-``) authenticates the socket;
        ``bot_token`` (``xoxb-``) backs the web client the SDK uses for responses.
        ``handler`` receives each translated interaction; ``mention_handler``, when
        given, receives each translated ``@radar`` mention."""
        self._handler = handler
        self._mention_handler = mention_handler
        self._client = SocketModeClient(
            app_token=app_token,
            web_client=AsyncWebClient(token=bot_token),
        )
        self._client.socket_mode_request_listeners.append(self._on_request)  # type: ignore[arg-type]

    async def _on_request(
        self, client: SocketModeClient, request: SocketModeRequest
    ) -> None:
        await ack_and_dispatch(client, request, self._handler, self._mention_handler)

    async def start(self) -> None:
        """Open the Socket Mode connection and begin receiving interactions."""
        # slack_sdk's SocketModeClient methods are unannotated; strict mypy flags
        # the call into vendor code.
        await self._client.connect()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        """Disconnect the Socket Mode connection."""
        await self._client.close()  # type: ignore[no-untyped-call]
