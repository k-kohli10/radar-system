"""Shared fixtures for the radar_database test suite.

These tests exercise real Postgres behaviour — transactions, ``FOR UPDATE SKIP
LOCKED``, unique constraints — so they need a running Postgres and skip when one
is unavailable. The setup itself lives in :mod:`radar_testing.postgres`, shared
with the other suites that need it.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
