"""Shared fixtures for the radar_watcher_agent test suite.

The watcher's guarantees are Postgres guarantees — the ``processed_events`` gate is
a real row with a real composite primary key, and "the duplicate did no work" is only
provable against a real database. So these tests need a running Postgres and skip
when there is none. The setup itself lives in :mod:`radar_testing.postgres`, shared
with every other suite that needs it.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
