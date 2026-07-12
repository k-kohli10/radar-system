"""Vendor-neutral alert normalization.

Each source has its own vendor payload shape; this module maps each to the one
vendor-neutral :class:`radar_contracts.NormalizedAlert` the rest of the pipeline
reasons over, and computes the correlation fingerprint
``sha256(service_name:alert_name:severity)``.

**One alert per request.** Ingestion normalizes exactly one alert per POST and
the pipeline opens exactly one incident per new fingerprint (the plan's singular
"202 with incident_id"). Prometheus alertmanager batches multiple alerts into a
single webhook by default, so RADAR configures alertmanager to *fan out* one
alert per POST — a receiver whose grouping makes each alert its own group (see
ADR 0011 and the ingestion README). A batched alertmanager envelope — a body
carrying an ``alerts`` array — is therefore a misconfiguration: it is rejected
with :class:`~radar_common.InvalidPayloadError` (→ 422), never silently
truncated to ``alerts[0]`` and never a crash.

Every field access is guarded: a missing ``service``/``alert``/``severity``, a
wrong-typed field, or an unparseable timestamp likewise raises
``InvalidPayloadError``, which the route maps to 422. Normalization never trusts
the payload's shape.

Expected per-source shapes:

- **prometheus** — a single alertmanager alert object::

      {"status": "firing",
       "labels": {"alertname": "...", "service": "...", "severity": "..."},
       "annotations": {"summary": "..."},
       "startsAt": "2026-07-09T10:30:00Z", "endsAt": "...",
       "fingerprint": "<alertmanager id>"}

- **kibana** — a RADAR-native Kibana Watcher webhook body::

      {"service_name": "...", "alert_name": "...", "severity": "...",
       "status": "firing", "triggered_at": "2026-07-09T10:30:00Z",
       "watch_id": "...", "labels": {...}, "annotations": {...}}

- **mock** — a minimal test body (``fired_at`` optional, defaults to now)::

      {"service_name": "...", "alert_name": "...", "severity": "critical"}
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from radar_common import InvalidPayloadError, ensure_utc, utcnow
from radar_contracts import NormalizedAlert, Severity


class AlertSource(StrEnum):
    """The alert sources ingestion accepts, one per ``/alerts/*`` endpoint."""

    PROMETHEUS = "prometheus"
    KIBANA = "kibana"
    MOCK = "mock"


def compute_fingerprint(service_name: str, alert_name: str, severity: str) -> str:
    """Return the correlation fingerprint for an alert.

    ``sha256(service_name + ":" + alert_name + ":" + severity)`` as a 64-char
    hex digest, matching the ``alerts.fingerprint`` column and the watcher's
    correlation key.
    """
    raw = f"{service_name}:{alert_name}:{severity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize(
    source: AlertSource,
    payload: Mapping[str, Any],
    *,
    correlation_id: UUID | None = None,
) -> NormalizedAlert:
    """Normalize a source payload to a :class:`NormalizedAlert`.

    ``correlation_id`` threads the request's id onto the alert (and thus onto
    the incident and outbox event it produces); when omitted the contract
    generates one. Raises :class:`~radar_common.InvalidPayloadError` (→ 422) on
    any malformed or unexpected payload.
    """
    if source is AlertSource.PROMETHEUS:
        return _normalize_prometheus(payload, correlation_id)
    if source is AlertSource.KIBANA:
        return _normalize_kibana(payload, correlation_id)
    return _normalize_mock(payload, correlation_id)


def _normalize_prometheus(
    payload: Mapping[str, Any], correlation_id: UUID | None
) -> NormalizedAlert:
    if isinstance(payload.get("alerts"), list):
        raise InvalidPayloadError(
            "prometheus payload is a batched alertmanager webhook (it has an "
            "'alerts' array); configure alertmanager to fan out one alert per "
            "POST. See ADR 0011."
        )
    labels = payload.get("labels")
    if not isinstance(labels, dict):
        raise InvalidPayloadError("prometheus payload missing a 'labels' object")

    service_name = _require_str(labels, "service", where="prometheus labels")
    alert_name = _require_str(labels, "alertname", where="prometheus labels")
    severity = _require_severity(labels, "severity", where="prometheus labels")
    status = _optional_str(payload, "status") or "firing"
    fired_at = _parse_timestamp(
        _require_str(payload, "startsAt", where="prometheus payload")
    )
    resolved_at = None
    if status == "resolved":
        ends_at = payload.get("endsAt")
        if isinstance(ends_at, str) and ends_at:
            resolved_at = _parse_timestamp(ends_at)

    return _build(
        source=AlertSource.PROMETHEUS,
        source_alert_id=_optional_str(payload, "fingerprint"),
        service_name=service_name,
        alert_name=alert_name,
        severity=severity,
        status=status,
        fired_at=fired_at,
        resolved_at=resolved_at,
        labels=_string_map(labels),
        annotations=_string_map(payload.get("annotations")),
        raw_payload=payload,
        correlation_id=correlation_id,
    )


def _normalize_kibana(
    payload: Mapping[str, Any], correlation_id: UUID | None
) -> NormalizedAlert:
    service_name = _require_str(payload, "service_name", where="kibana payload")
    alert_name = _require_str(payload, "alert_name", where="kibana payload")
    severity = _require_severity(payload, "severity", where="kibana payload")
    status = _optional_str(payload, "status") or "firing"
    fired_at = _parse_timestamp(
        _require_str(payload, "triggered_at", where="kibana payload")
    )

    return _build(
        source=AlertSource.KIBANA,
        source_alert_id=_optional_str(payload, "watch_id"),
        service_name=service_name,
        alert_name=alert_name,
        severity=severity,
        status=status,
        fired_at=fired_at,
        resolved_at=None,
        labels=_string_map(payload.get("labels")),
        annotations=_string_map(payload.get("annotations")),
        raw_payload=payload,
        correlation_id=correlation_id,
    )


def _normalize_mock(
    payload: Mapping[str, Any], correlation_id: UUID | None
) -> NormalizedAlert:
    service_name = _require_str(payload, "service_name", where="mock payload")
    alert_name = _require_str(payload, "alert_name", where="mock payload")
    severity = _require_severity(payload, "severity", where="mock payload")
    status = _optional_str(payload, "status") or "firing"
    fired_raw = _optional_str(payload, "fired_at")
    fired_at = _parse_timestamp(fired_raw) if fired_raw else utcnow()

    return _build(
        source=AlertSource.MOCK,
        source_alert_id=_optional_str(payload, "source_alert_id"),
        service_name=service_name,
        alert_name=alert_name,
        severity=severity,
        status=status,
        fired_at=fired_at,
        resolved_at=None,
        labels=_string_map(payload.get("labels")),
        annotations=_string_map(payload.get("annotations")),
        raw_payload=payload,
        correlation_id=correlation_id,
    )


def _build(
    *,
    source: AlertSource,
    source_alert_id: str | None,
    service_name: str,
    alert_name: str,
    severity: Severity,
    status: str,
    fired_at: datetime,
    resolved_at: datetime | None,
    labels: dict[str, str],
    annotations: dict[str, str],
    raw_payload: Mapping[str, Any],
    correlation_id: UUID | None,
) -> NormalizedAlert:
    """Assemble a :class:`NormalizedAlert`, computing the fingerprint.

    Fields the contract defaults (``id``, ``received_at``) are left to it;
    ``correlation_id`` is only overridden when the caller threads one in.
    """
    overrides: dict[str, Any] = {}
    if correlation_id is not None:
        overrides["correlation_id"] = correlation_id
    return NormalizedAlert(
        source=source.value,
        source_alert_id=source_alert_id,
        fingerprint=compute_fingerprint(service_name, alert_name, severity.value),
        service_name=service_name,
        alert_name=alert_name,
        severity=severity,
        status=status,
        raw_payload=dict(raw_payload),
        labels=labels,
        annotations=annotations,
        fired_at=fired_at,
        resolved_at=resolved_at,
        **overrides,
    )


def _require_str(payload: Mapping[str, Any], key: str, *, where: str) -> str:
    """Return ``payload[key]`` as a non-empty string, or raise (→ 422)."""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidPayloadError(
            f"{where} missing required non-empty string field '{key}'"
        )
    return value


def _require_severity(payload: Mapping[str, Any], key: str, *, where: str) -> Severity:
    """Return ``payload[key]`` as a canonical :class:`Severity`, or raise (→ 422).

    Severity is a closed vocabulary: an unknown value (e.g. ``'warning'``,
    ``'page'``, ``'P1'``) is a source-configuration error, rejected with a
    message listing the allowed set — never silently mapped or floored, so
    equal severities always compare equal downstream.
    """
    value = _require_str(payload, key, where=where)
    try:
        return Severity(value)
    except ValueError:
        allowed = ", ".join(s.value for s in Severity)
        raise InvalidPayloadError(
            f"{where} has unknown severity {value!r}; expected one of: {allowed}"
        ) from None


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    """Return ``payload[key]`` as a string if present, else None; raise on the
    wrong type (a silently-coerced id would be a lie about the source)."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidPayloadError(f"field '{key}' must be a string")
    return value


def _string_map(value: Any) -> dict[str, str]:
    """Coerce an optional object of key/values to ``dict[str, str]``.

    ``None`` becomes an empty map; a non-object raises (→ 422). Keys and values
    are stringified so vendor labels with non-string values still normalize.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidPayloadError("expected an object of string key/value pairs")
    return {str(k): str(v) for k, v in value.items()}


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp to timezone-aware UTC, or raise (→ 422)."""
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        raise InvalidPayloadError(
            f"unparseable ISO-8601 timestamp: {value!r}"
        ) from None
