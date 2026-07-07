"""Timezone-aware UTC helpers.

RADAR stores every timestamp in ``TIMESTAMPTZ`` columns and threads timestamps
through JSON event payloads, so times must always be timezone-aware UTC — never
naive. :func:`utcnow` is the single canonical "now" (replacing the deprecated,
naive ``datetime.utcnow()``); :func:`ensure_utc` normalizes timestamps arriving
from external sources (e.g. an alert's ``fired_at``) before they are persisted.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a timezone-aware UTC datetime.

    A naive datetime is assumed to already be in UTC and tagged as such; an
    aware datetime is converted to UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
