"""Slack implementation of the RADAR notification backend contract.

Structural implementation of ``radar_contracts.NotificationBackend`` over the
slack-sdk async Web API client. Portable by design: it depends on
``radar-contracts`` and the ``slack_sdk`` SDK only, and never imports the
plugin-sdk or any RADAR service. The consuming application registers this class
with its own plugin registry and constructs it from config via the plugin-sdk
loader.

One method, ``send``, mapping the vendor-neutral call onto ``chat.postMessage``:
``blocks`` carries the RCA card's rich layout, ``thread_ref`` becomes
``thread_ts`` so bot replies thread under the card, and the returned message
timestamp is the reference the caller stores to link feedback back to the message.

``text`` is always sent even when ``blocks`` is present. Slack uses it as the
notification/fallback text — in push notifications, the sidebar, and any client
that cannot render blocks — and a blocks-only message shows up blank in exactly
those places. It is a fallback, not a duplicate.

**Errors are surfaced, never swallowed.** A failed post must reach the caller,
because the caller's whole job is deciding whether the RCA card was delivered;
a backend that returned quietly on failure would let an incident be recorded as
delivered when nobody was told. ``SlackApiError`` propagates as-is, and a
response missing the message timestamp raises rather than returning a sentinel.

The bot token is a constructor argument, not read here: RADAR sources it from a
Vault-mounted file at the application edge and passes the value in, so this
plugin holds no opinion about secret storage.

POC scope: a correct single-message post. Rate-limit backoff and connection
pooling are deferred to Phase 13.
"""

from __future__ import annotations

from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

BACKEND = "slack"
"""Registry name this backend registers under for ``NotificationBackend``."""

DEFAULT_TIMEOUT_SECONDS = 10
"""Per-request wall-clock budget for a Slack Web API call."""


class SlackNotificationBackend:
    """``NotificationBackend`` over the Slack Web API."""

    def __init__(
        self,
        *,
        token: str,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """Bind to a Slack workspace with a bot token.

        ``token`` is the bot user OAuth token (``xoxb-``), read by the caller from
        its Vault-mounted secret file. ``timeout`` bounds each Web API request so
        a hung Slack call cannot stall the delivery path indefinitely.
        """
        self._client = AsyncWebClient(token=token, timeout=timeout)

    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        """Post ``text`` (optionally as ``blocks``) to ``channel``; return its ``ts``.

        ``thread_ref`` posts as a threaded reply under that message reference.
        The returned Slack message timestamp is the durable handle the caller
        records — RADAR stores it on the recommendation so the card can be found,
        threaded under, and linked to feedback later.

        Raises ``SlackApiError`` if Slack rejects the post, and ``ValueError`` if
        Slack accepts it but returns no timestamp — the caller is about to treat
        this as proof of delivery, so an unusable reference must fail loudly
        rather than be stored as an empty string.
        """
        response = await self._client.chat_postMessage(
            channel=channel,
            text=text,
            blocks=blocks,
            thread_ts=thread_ref,
        )
        message_ts = response.get("ts")
        if not isinstance(message_ts, str) or not message_ts:
            raise ValueError(
                "slack accepted the message but returned no 'ts'; "
                "cannot record delivery without a message reference"
            )
        return message_ts
