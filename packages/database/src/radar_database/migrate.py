"""Run Alembic migrations from the installed package (container-friendly).

``make migrate`` runs alembic from ``packages/database/`` against its
``alembic.ini`` (whose ``script_location`` is a repo-relative path). In a
container only the installed package exists, so build the Alembic config
programmatically here — pointing ``script_location`` at the ``migrations``
directory inside this package — and run ``upgrade``. The migration environment
(``migrations/env.py``) reads the database URL from ``POSTGRES_DSN``, the same
variable the Makefile passes, so nothing else needs configuring.

Usage (e.g. the k8s migration Job)::

    POSTGRES_DSN=postgresql://... python -m radar_database.migrate        # -> head
    POSTGRES_DSN=postgresql://... python -m radar_database.migrate <rev>
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config() -> Config:
    """Alembic config resolved from inside the installed package."""
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    return cfg


def main() -> None:
    revision = sys.argv[1] if len(sys.argv) > 1 else "head"
    command.upgrade(_config(), revision)


if __name__ == "__main__":
    main()
