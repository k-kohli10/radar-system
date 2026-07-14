"""Shared fixtures for the radar_reasoner_agent test suite.

The reasoner's guarantees are Postgres guarantees — the ``processed_events`` gate is
a real row with a real composite primary key, ``recommendations`` has a real unique
index (one RCA per incident), and "the duplicate did no work" is only provable
against a real database. So these tests need a running Postgres and skip when there
is none. The setup itself lives in :mod:`radar_testing.postgres`.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
