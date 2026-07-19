"""Real-Postgres fixtures for the knowledge service suite.

The manifest is what makes a re-run cheap, and skipping an unchanged file
depends on a hash actually surviving a round trip through Postgres. The setup
lives in :mod:`radar_testing.postgres`; these tests skip when no database is
configured.
"""

from __future__ import annotations

from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
