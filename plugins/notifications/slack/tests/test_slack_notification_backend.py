"""Behavioral tests for the Slack notification backend (mocked client).

Conformance (``isinstance`` against the runtime-checkable Protocol) only proves
``SlackNotificationBackend`` is *shaped* like ``NotificationBackend``. These prove
it *behaves*: it calls ``chat.postMessage`` with the mapped arguments, always
sends fallback ``text`` alongside ``blocks``, maps ``thread_ref`` onto
``thread_ts``, returns the Slack message timestamp, and surfaces failures instead
of swallowing them. No real Slack — the async client is mocked, so this is a cheap
unit test.

The two failure tests are the load-bearing ones. This backend's return value is
about to be stored as PROOF that an on-call engineer was told about an incident,
so "Slack rejected it" and "Slack returned no timestamp" must both raise. A
backend that returned quietly on either would let RADAR record a delivery that
never happened.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from radar_contracts import NotificationBackend
from radar_plugin_notifications_slack import SlackNotificationBackend
from slack_sdk.errors import SlackApiError

CLIENT_PATH = "radar_plugin_notifications_slack.backend.AsyncWebClient"

CARD_BLOCKS: list[dict[str, Any]] = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "*Root cause*"}},
]


def _backend(client_cls: Any, *, response: Any) -> SlackNotificationBackend:
    """Build a backend whose mocked client returns ``response`` from postMessage."""
    client_cls.return_value.chat_postMessage = AsyncMock(return_value=response)
    return SlackNotificationBackend(token="xoxb-test")


def test_satisfies_the_notification_backend_protocol() -> None:
    """Structural conformance against the runtime-checkable Protocol."""
    with patch(CLIENT_PATH):
        backend = SlackNotificationBackend(token="xoxb-test")
    assert isinstance(backend, NotificationBackend)


async def test_send_posts_message_and_returns_timestamp() -> None:
    with patch(CLIENT_PATH) as client_cls:
        backend = _backend(client_cls, response={"ok": True, "ts": "1720519200.001"})
        result = await backend.send(
            "#incidents", "RCA ready", blocks=CARD_BLOCKS, thread_ref=None
        )

    post = client_cls.return_value.chat_postMessage
    post.assert_awaited_once()
    kwargs = post.call_args.kwargs
    assert kwargs["channel"] == "#incidents"
    assert kwargs["blocks"] == CARD_BLOCKS
    assert kwargs["thread_ts"] is None
    # The Slack message timestamp, returned verbatim — this is the reference the
    # caller stores to prove and later locate the delivery.
    assert result == "1720519200.001"


async def test_fallback_text_is_sent_even_with_blocks() -> None:
    """``text`` accompanies ``blocks``, never replaced by them.

    Slack uses ``text`` for push notifications and the sidebar preview. A
    blocks-only message renders blank there — the on-call engineer's phone would
    buzz with an empty notification for an incident.
    """
    with patch(CLIENT_PATH) as client_cls:
        backend = _backend(client_cls, response={"ok": True, "ts": "1.0"})
        await backend.send("#incidents", "RCA ready", blocks=CARD_BLOCKS)

    kwargs = client_cls.return_value.chat_postMessage.call_args.kwargs
    assert kwargs["text"] == "RCA ready"
    assert kwargs["blocks"] == CARD_BLOCKS


async def test_thread_ref_maps_to_thread_ts() -> None:
    """A threaded reply posts under the parent message reference."""
    with patch(CLIENT_PATH) as client_cls:
        backend = _backend(client_cls, response={"ok": True, "ts": "2.0"})
        await backend.send("#incidents", "reply", thread_ref="1720519200.001")

    kwargs = client_cls.return_value.chat_postMessage.call_args.kwargs
    assert kwargs["thread_ts"] == "1720519200.001"


async def test_slack_api_error_propagates() -> None:
    """A rejected post raises — the caller must not record a delivery.

    ``channel_not_found`` is the realistic case: a misconfigured channel name.
    Swallowing it would mark the incident delivered while nobody was told.
    """
    with patch(CLIENT_PATH) as client_cls:
        client_cls.return_value.chat_postMessage = AsyncMock(
            # slack-sdk does not annotate SlackApiError.__init__, so strict mypy
            # sees an untyped call into vendor code.
            side_effect=SlackApiError(  # type: ignore[no-untyped-call]
                "channel_not_found", response={"ok": False}
            )
        )
        backend = SlackNotificationBackend(token="xoxb-test")
        with pytest.raises(SlackApiError):
            await backend.send("#nope", "RCA ready", blocks=CARD_BLOCKS)


@pytest.mark.parametrize("bad", [{"ok": True}, {"ok": True, "ts": ""}])
async def test_missing_or_empty_timestamp_raises(bad: dict[str, Any]) -> None:
    """Accepted but no usable ``ts`` is a failure, not an empty string.

    The caller stores this value as the delivery reference. Returning ``""`` would
    persist a message reference that points at nothing, and the no-double-post
    guard downstream would then be keyed on a meaningless value.
    """
    with patch(CLIENT_PATH) as client_cls:
        backend = _backend(client_cls, response=bad)
        with pytest.raises(ValueError, match="no 'ts'"):
            await backend.send("#incidents", "RCA ready")
