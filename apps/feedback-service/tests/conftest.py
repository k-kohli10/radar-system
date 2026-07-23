"""Shared fixtures for the radar_feedback_service test suite.

The feedback-service's gate is a Postgres guarantee — the ``processed_events``
row with its real composite primary key, and "the duplicate reached no work" is
only provable against a real database. So these tests need a running Postgres and
skip when there is none. The setup itself lives in :mod:`radar_testing.postgres`,
shared with every other suite that needs it.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
