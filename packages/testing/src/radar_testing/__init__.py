"""RADAR shared test support.

Test-only fixtures, consumed as a dev dependency so they never enter a runtime
package's import surface. See :mod:`radar_testing.postgres` for the real-Postgres
fixtures (``database_url``, ``db``).
"""

from __future__ import annotations

__version__ = "0.6.0"

__all__ = ["__version__"]
