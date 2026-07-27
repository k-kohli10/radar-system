"""RADAR feedback service.

The last stage of the incident pipeline and the only one an engineer sees. It
consumes ``recommendation.created`` from the outbox, delivers the RCA card to
Slack, and (in later commits) handles the interactive callbacks and bot commands
that come back. One deployment, one Slack connection.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
