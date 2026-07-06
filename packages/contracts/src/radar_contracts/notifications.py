"""Notification backend contract.

``NotificationBackend`` is the vendor-neutral interface for delivering
notifications (RADAR uses it to send Slack RCA cards and threaded bot replies).
It is a ``typing.Protocol`` (structural), never an ABC, and references no vendor
type. The Slack implementation lives in ``plugins/notifications/slack/`` and
imports ``slack-sdk`` there; nothing in this module does.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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
