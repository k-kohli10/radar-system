"""RADAR Slack notification backend plugin.

Structural implementation of the ``radar-contracts`` ``NotificationBackend``
protocol over the slack-sdk async Web API client (which also pulls in
``aiohttp``), plus the Socket Mode receive side.
"""

from __future__ import annotations

from .backend import BACKEND, DEFAULT_TIMEOUT_SECONDS, SlackNotificationBackend
from .interactions import (
    InteractionHandler,
    MentionHandler,
    SlackSocketSource,
    ack_and_dispatch,
    to_interaction,
    to_mention,
)

__version__ = "0.1.0"

__all__ = [
    "BACKEND",
    "DEFAULT_TIMEOUT_SECONDS",
    "InteractionHandler",
    "MentionHandler",
    "SlackNotificationBackend",
    "SlackSocketSource",
    "ack_and_dispatch",
    "to_interaction",
    "to_mention",
    "__version__",
]
