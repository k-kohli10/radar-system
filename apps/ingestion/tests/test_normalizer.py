"""Normalizer tests: per-source mapping, the fingerprint, and 422 rejections.

The normalizer is the pipeline's trust boundary — every source payload is mapped
to the one vendor-neutral :class:`NormalizedAlert` and anything malformed is
rejected with :class:`~radar_common.InvalidPayloadError` (the route maps it to
422). These tests pin: (1) each source shape normalizes to the expected fields,
(2) the ``sha256(service:alert:severity)`` fingerprint is exactly that and is
source-independent, (3) a batched alertmanager envelope is rejected rather than
silently truncated, (4) missing/wrong-typed/unparseable fields are rejected, and
(5) severity is a closed vocabulary — an unknown value is a 422, never mapped.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from radar_common import InvalidPayloadError
from radar_contracts import NormalizedAlert, Severity
from radar_ingestion.normalizer import (
    AlertSource,
    compute_fingerprint,
    normalize,
)


def _expected_fingerprint(service: str, alert: str, severity: str) -> str:
    return hashlib.sha256(f"{service}:{alert}:{severity}".encode()).hexdigest()


# --- per-source mapping -------------------------------------------------------


def test_prometheus_payload_normalizes_to_expected_fields() -> None:
    payload = {
        "status": "firing",
        "labels": {
            "alertname": "OrderProcessingFailureRate",
            "service": "order-service",
            "severity": "critical",
            "region": "us-east-1",
        },
        "annotations": {"summary": "Order failures over threshold"},
        "startsAt": "2026-07-09T10:30:00Z",
        "fingerprint": "amgr-abc123",
    }

    alert = normalize(AlertSource.PROMETHEUS, payload)

    assert isinstance(alert, NormalizedAlert)
    assert alert.source == "prometheus"
    assert alert.source_alert_id == "amgr-abc123"
    assert alert.service_name == "order-service"
    assert alert.alert_name == "OrderProcessingFailureRate"
    assert alert.severity is Severity.CRITICAL
    assert alert.status == "firing"
    assert alert.fired_at == datetime(2026, 7, 9, 10, 30, tzinfo=UTC)
    assert alert.resolved_at is None
    assert alert.labels["region"] == "us-east-1"
    assert alert.annotations["summary"] == "Order failures over threshold"
    assert alert.raw_payload == payload


def test_prometheus_resolved_status_carries_resolved_at() -> None:
    payload = {
        "status": "resolved",
        "labels": {
            "alertname": "HighLatency",
            "service": "checkout",
            "severity": "high",
        },
        "startsAt": "2026-07-09T10:30:00Z",
        "endsAt": "2026-07-09T10:45:00Z",
    }

    alert = normalize(AlertSource.PROMETHEUS, payload)

    assert alert.status == "resolved"
    assert alert.resolved_at == datetime(2026, 7, 9, 10, 45, tzinfo=UTC)


def test_kibana_payload_normalizes_to_expected_fields() -> None:
    payload = {
        "service_name": "search-service",
        "alert_name": "IndexingLag",
        "severity": "medium",
        "status": "firing",
        "triggered_at": "2026-07-09T11:00:00Z",
        "watch_id": "watch-77",
        "labels": {"cluster": "primary"},
        "annotations": {"runbook": "https://rb/indexing-lag"},
    }

    alert = normalize(AlertSource.KIBANA, payload)

    assert alert.source == "kibana"
    assert alert.source_alert_id == "watch-77"
    assert alert.service_name == "search-service"
    assert alert.alert_name == "IndexingLag"
    assert alert.severity is Severity.MEDIUM
    assert alert.fired_at == datetime(2026, 7, 9, 11, 0, tzinfo=UTC)
    assert alert.labels == {"cluster": "primary"}
    assert alert.annotations == {"runbook": "https://rb/indexing-lag"}


def test_mock_payload_normalizes_and_defaults_fired_at_to_now() -> None:
    before = datetime.now(UTC)
    payload = {
        "service_name": "billing",
        "alert_name": "PaymentDeclineSpike",
        "severity": "low",
    }

    alert = normalize(AlertSource.MOCK, payload)
    after = datetime.now(UTC)

    assert alert.source == "mock"
    assert alert.source_alert_id is None
    assert alert.severity is Severity.LOW
    assert alert.status == "firing"
    # fired_at omitted → stamped with receive-time now, in [before, after].
    assert before <= alert.fired_at <= after


# --- fingerprint --------------------------------------------------------------


def test_fingerprint_is_sha256_of_service_alert_severity() -> None:
    fp = compute_fingerprint("order-service", "OrderFailure", "critical")
    assert fp == _expected_fingerprint("order-service", "OrderFailure", "critical")
    assert len(fp) == 64


def test_fingerprint_is_source_independent_for_equal_identity() -> None:
    prometheus = normalize(
        AlertSource.PROMETHEUS,
        {
            "labels": {
                "alertname": "OrderFailure",
                "service": "order-service",
                "severity": "critical",
            },
            "startsAt": "2026-07-09T10:30:00Z",
        },
    )
    kibana = normalize(
        AlertSource.KIBANA,
        {
            "service_name": "order-service",
            "alert_name": "OrderFailure",
            "severity": "critical",
            "triggered_at": "2026-07-09T11:59:00Z",
        },
    )

    # Same service:alert:severity → same correlation fingerprint regardless of
    # source or fired_at, so the two dedup onto one incident downstream.
    assert prometheus.fingerprint == kibana.fingerprint
    assert prometheus.fingerprint == _expected_fingerprint(
        "order-service", "OrderFailure", "critical"
    )


# --- batched envelope is a misconfiguration, not alerts[0] --------------------


def test_prometheus_batched_envelope_is_rejected_not_truncated() -> None:
    batched = {
        "alerts": [
            {
                "labels": {
                    "alertname": "A",
                    "service": "svc",
                    "severity": "high",
                },
                "startsAt": "2026-07-09T10:30:00Z",
            }
        ]
    }

    with pytest.raises(InvalidPayloadError):
        normalize(AlertSource.PROMETHEUS, batched)


# --- malformed payloads → 422 -------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"startsAt": "2026-07-09T10:30:00Z"},
            id="missing-labels",
        ),
        pytest.param(
            {
                "labels": {"service": "svc", "severity": "high"},
                "startsAt": "2026-07-09T10:30:00Z",
            },
            id="missing-alertname",
        ),
        pytest.param(
            {
                "labels": {
                    "alertname": "A",
                    "service": "svc",
                    "severity": "high",
                }
            },
            id="missing-startsAt",
        ),
        pytest.param(
            {
                "labels": {
                    "alertname": "A",
                    "service": "svc",
                    "severity": "high",
                },
                "startsAt": "not-a-timestamp",
            },
            id="unparseable-timestamp",
        ),
        pytest.param(
            {
                "labels": {
                    "alertname": 123,
                    "service": "svc",
                    "severity": "high",
                },
                "startsAt": "2026-07-09T10:30:00Z",
            },
            id="wrong-typed-alertname",
        ),
    ],
)
def test_malformed_prometheus_payload_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(InvalidPayloadError):
        normalize(AlertSource.PROMETHEUS, payload)


# --- severity is a closed vocabulary → unknown is 422 -------------------------


@pytest.mark.parametrize("bad", ["warning", "page", "P1", "CRITICAL", ""])
def test_unknown_severity_is_rejected(bad: str) -> None:
    payload = {
        "service_name": "svc",
        "alert_name": "A",
        "severity": bad,
        "triggered_at": "2026-07-09T10:30:00Z",
    }
    with pytest.raises(InvalidPayloadError):
        normalize(AlertSource.KIBANA, payload)


@pytest.mark.parametrize("value", [s.value for s in Severity])
def test_each_canonical_severity_is_accepted(value: str) -> None:
    payload = {
        "service_name": "svc",
        "alert_name": "A",
        "severity": value,
        "triggered_at": "2026-07-09T10:30:00Z",
    }
    alert = normalize(AlertSource.KIBANA, payload)
    assert alert.severity is Severity(value)
