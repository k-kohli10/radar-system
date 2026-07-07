"""Timezone-aware UTC helper tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from radar_common import ensure_utc, utcnow


def test_utcnow_is_timezone_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_ensure_utc_tags_naive_as_utc() -> None:
    naive = datetime(2026, 7, 6, 12, 0, 0)
    result = ensure_utc(naive)
    assert result.tzinfo is UTC
    assert result.hour == 12


def test_ensure_utc_converts_offset_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2026, 7, 6, 12, 0, 0, tzinfo=ist)
    result = ensure_utc(aware)
    assert result.utcoffset() == timedelta(0)
    assert result.hour == 6
    assert result.minute == 30
