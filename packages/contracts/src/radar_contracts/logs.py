"""Logs backend contract.

``LogsBackend`` is the vendor-neutral interface for querying service logs. RADAR
ships structured logs to Elasticsearch, but this ``typing.Protocol`` (never an
ABC) references no vendor type; the Elasticsearch client lives in
``plugins/logs/elastic/`` and imports its SDK there, not here.

Log entries are returned as plain dictionaries so the contract stays free of any
backend-specific document shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LogsBackend(Protocol):
    """Interface for a log query backend."""

    async def query(
        self,
        service_name: str,
        *,
        query: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return matching log entries for a service, most recent first.

        ``query`` is an optional free-text/keyword filter; ``start`` and ``end``
        bound the time window; ``limit`` caps the number of entries returned.
        """
        ...
