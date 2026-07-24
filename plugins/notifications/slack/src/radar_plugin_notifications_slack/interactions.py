"""Receive side: interactive callbacks over Socket Mode.

The send side (:mod:`backend`) posts and updates cards; this is the reverse — a
user clicking a button on the card. :class:`SlackInteractionSource` opens a Socket
Mode connection and hands each button click to the app as a vendor-neutral
:class:`~radar_contracts.NotificationInteraction`; feedback-service alone
interprets it.

**Socket Mode, and what it does (and does not) authenticate.** The source opens an
OUTBOUND WebSocket to Slack authenticated by the app-level token (``xapp-``). Slack
delivers only THIS app's own interactions over that connection — the authenticated
socket IS the authentication. There is deliberately no request-signature check
here: signing (``X-Slack-Signature``) is the HTTP Events API's mechanism, and the
property Socket Mode gives for free — only Slack can deliver here — is exactly what
a public HTTP endpoint would lose. When Phase 12 moves to Events API + ingress,
signing becomes mandatory; until then it does not apply.

The translation from Slack's ``block_actions`` payload to the neutral contract is
the pure :func:`to_interaction`; the socket wiring around it is the thin shell.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from radar_contracts import NotificationInteraction
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

InteractionHandler = Callable[[NotificationInteraction], Awaitable[None]]
"""What the app registers: handles one interaction. May take as long as it needs —
the envelope is already acked by the time it runs (see below)."""

_INTERACTIVE = "interactive"
_BLOCK_ACTIONS = "block_actions"


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


async def ack_and_dispatch(
    client: Any,
    request: SocketModeRequest,
    handler: InteractionHandler,
) -> None:
    """Ack the envelope, then dispatch a translated interaction to ``handler``.

    The whole logic of the receive side, separated from the vendor client wiring so
    it is testable with a fake ``client`` and no Socket Mode connection.

    The ack goes FIRST, before any work, inside Slack's ~3s window — an unacked
    envelope is redelivered. Processing therefore happens after the ack, so a
    handler that fails does NOT get the envelope redelivered: a lost 👍/👎 is a lost
    signal (harmless), and a lost Resolve is recoverable (the engineer can click
    again, and the alert-resolved webhook resolves it anyway). Prompt ack over
    guaranteed processing is the deliberate trade for an interactive control; see the
    phase notes on interaction idempotency.
    """
    await client.send_socket_mode_response(
        SocketModeResponse(envelope_id=request.envelope_id)
    )
    if request.type != _INTERACTIVE:
        return
    interaction = to_interaction(request.payload)
    if interaction is None:
        return
    await handler(interaction)


class SlackInteractionSource:
    """A Socket Mode connection delivering button clicks as ``NotificationInteraction``.

    Constructing this opens NO network connection; :meth:`start` connects and
    :meth:`close` disconnects, so the app owns the lifecycle in its lifespan. The
    ack/translate/dispatch logic lives in :func:`ack_and_dispatch`; this class is
    the thin wiring that registers it as a Socket Mode listener.
    """

    def __init__(
        self,
        *,
        app_token: str,
        bot_token: str,
        handler: InteractionHandler,
    ) -> None:
        """Bind to Slack. ``app_token`` (``xapp-``) authenticates the socket;
        ``bot_token`` (``xoxb-``) backs the web client the SDK uses for responses.
        ``handler`` receives each translated interaction."""
        self._handler = handler
        self._client = SocketModeClient(
            app_token=app_token,
            web_client=AsyncWebClient(token=bot_token),
        )
        self._client.socket_mode_request_listeners.append(self._on_request)  # type: ignore[arg-type]

    async def _on_request(
        self, client: SocketModeClient, request: SocketModeRequest
    ) -> None:
        await ack_and_dispatch(client, request, self._handler)

    async def start(self) -> None:
        """Open the Socket Mode connection and begin receiving interactions."""
        # slack_sdk's SocketModeClient methods are unannotated; strict mypy flags
        # the call into vendor code.
        await self._client.connect()  # type: ignore[no-untyped-call]

    async def close(self) -> None:
        """Disconnect the Socket Mode connection."""
        await self._client.close()  # type: ignore[no-untyped-call]
