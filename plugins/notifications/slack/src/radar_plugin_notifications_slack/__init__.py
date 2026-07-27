"""RADAR Slack notification backend plugin.

Portable structural implementation of the ``radar-contracts``
``NotificationBackend`` protocol over the slack-sdk async Web API client. Depends
only on ``radar-contracts`` and the ``slack_sdk`` client (plus ``aiohttp``, which
its async client requires); the consuming application registers the class with
its own plugin registry and constructs it via the plugin-sdk loader.
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
