"""Shared fixtures for the radar_outbox_worker test suite.

The outbox worker's guarantees are Postgres guarantees: ``FOR UPDATE SKIP
LOCKED`` row locking, transaction-scoped ``NOW()``, and real concurrent
transactions. None of that exists against SQLite or a mock, so these tests need a
running Postgres and skip when one is unavailable. The setup itself lives in
:mod:`radar_testing.postgres`, shared with the other suites that need it.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
