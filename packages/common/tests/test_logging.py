"""Structured logging tests.

Assert JSON-to-stdout shape, that ``service`` and ``correlation_id`` land on
every line, and that a bound correlation id rides on subsequent lines.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
from radar_common import (
    bind_log_correlation_id,
    clear_context,
    configure_logging,
    get_logger,
)


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    configure_logging(service_name="watcher-agent")
    yield
    clear_context()


def _last_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out = capsys.readouterr().out.strip().splitlines()
    record: dict[str, object] = json.loads(out[-1])
    return record


def test_emits_json_with_service_and_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_logger("demo").info("incident.opened", incident_id="abc")
    record = _last_line(capsys)
    assert record["event"] == "incident.opened"
    assert record["service"] == "watcher-agent"
    assert record["incident_id"] == "abc"
    assert record["level"] == "info"
    assert "timestamp" in record


def test_correlation_id_present_even_without_binding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get_logger("demo").info("startup")
    assert _last_line(capsys)["correlation_id"] is None


def test_bound_correlation_id_rides_every_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cid = uuid4()
    bind_log_correlation_id(cid)
    log = get_logger("demo")
    log.info("first")
    log.info("second")
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert [line["correlation_id"] for line in lines] == [str(cid), str(cid)]


def test_clear_context_drops_correlation_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bind_log_correlation_id(uuid4())
    clear_context()
    get_logger("demo").info("after-clear")
    assert _last_line(capsys)["correlation_id"] is None
