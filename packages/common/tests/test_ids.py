"""UUID helper tests."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from radar_common import (
    InvalidPayloadError,
    new_correlation_id,
    new_event_id,
    new_id,
    parse_uuid,
)


@pytest.mark.parametrize("generator", [new_id, new_event_id, new_correlation_id])
def test_generators_return_unique_uuids(generator: Any) -> None:
    first, second = generator(), generator()
    assert isinstance(first, UUID)
    assert first != second


def test_parse_uuid_passes_through_uuid() -> None:
    cid = new_correlation_id()
    assert parse_uuid(cid) is cid


def test_parse_uuid_parses_string() -> None:
    text = "11111111-1111-1111-1111-111111111111"
    assert parse_uuid(text) == UUID(text)


@pytest.mark.parametrize("bad", ["nope", "", None, 123, 12.5, ["x"]])
def test_parse_uuid_rejects_malformed(bad: Any) -> None:
    with pytest.raises(InvalidPayloadError) as exc:
        parse_uuid(bad, field="event_id")
    assert "event_id" in str(exc.value)
