"""Transactional incident creation and outbox publish.

The heart of ingestion's write path. Given a normalized alert, :func:`persist_alert`
either attaches it to an already-open incident or opens a new one — and does so
in a single transaction the caller commits, so the incident, the alert, and (for
a new incident) the outbox event are all-or-nothing.

New incident (no open match within the dedup window):

- INSERT incident, INSERT alert (linked to it), and — reusing the shared outbox
  writer — INSERT one ``alert.normalized`` outbox event targeting the watcher
  agent, all in one transaction.

Duplicate (an open incident matched):

- INSERT the alert linked to that incident and bump its ``alert_count`` /
  ``updated_at``. **No outbox event** — the pipeline already started for this
  incident on the first alert, and duplicates never reach the watcher, so this
  is the only place their arrival is recorded.

This module does not flush for ordering and does not commit: it adds the rows and
the route owns the ``commit()`` boundary, so the incident, alert, and outbox event
are issued and FK-checked as one atomic unit (a failure rolls the whole unit
back). No manual flush ordering is needed — the parent->child FKs are deferrable
(checked at commit), so the alert may be written before its incident within the
transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from radar_common import new_id, utcnow
from radar_contracts import NormalizedAlert
from radar_database import Alert, Incident, write_outbox_event
from sqlalchemy.ext.asyncio import AsyncSession

from radar_ingestion.deduper import find_open_incident

ALERT_NORMALIZED_EVENT = "alert.normalized"
"""Outbox event type emitted when a new incident is opened."""

WATCHER_TARGET = "watcher-agent"
"""Target service for the ``alert.normalized`` event."""


@dataclass(frozen=True)
class PublishResult:
    """Outcome of persisting one alert."""

    incident_id: UUID
    deduplicated: bool


async def persist_alert(
    session: AsyncSession,
    alert: NormalizedAlert,
    *,
    as_of: datetime | None = None,
) -> PublishResult:
    """Attach ``alert`` to an open incident or open a new one, transactionally.

    ``as_of`` is the dedup reference time (defaults to the alert's
    ``received_at``). Adds rows but does not commit — the caller commits so the
    incident, alert, and outbox event are one atomic unit. Insert order is
    irrelevant because the FKs are deferrable.
    """
    reference = as_of if as_of is not None else alert.received_at
    existing = await find_open_incident(
        session, fingerprint=alert.fingerprint, as_of=reference
    )

    if existing is not None:
        existing.alert_count += 1
        existing.updated_at = utcnow()
        session.add(_alert_row(alert, existing.id))
        return PublishResult(incident_id=existing.id, deduplicated=True)

    incident_id = new_id()
    session.add(_new_incident(alert, incident_id))
    session.add(_alert_row(alert, incident_id))
    await write_outbox_event(
        session,
        event_type=ALERT_NORMALIZED_EVENT,
        target_service=WATCHER_TARGET,
        payload=alert.model_dump(mode="json"),
        correlation_id=alert.correlation_id,
    )
    return PublishResult(incident_id=incident_id, deduplicated=False)


def _new_incident(alert: NormalizedAlert, incident_id: UUID) -> Incident:
    """Build the incident row a first alert opens.

    ``correlation_id`` carries the alert's id (unique per inbound request, and
    the incidents table requires it unique). ``severity`` stores the canonical
    string value; ``alert_count`` starts at 1.
    """
    return Incident(
        id=incident_id,
        correlation_id=alert.correlation_id,
        fingerprint=alert.fingerprint,
        service_name=alert.service_name,
        title=f"{alert.service_name} {alert.alert_name}",
        severity=alert.severity.value,
        status="open",
        alert_count=1,
    )


def _alert_row(alert: NormalizedAlert, incident_id: UUID) -> Alert:
    """Build the alerts row for ``alert``, linked to ``incident_id``."""
    return Alert(
        id=alert.id,
        source=alert.source,
        source_alert_id=alert.source_alert_id,
        fingerprint=alert.fingerprint,
        service_name=alert.service_name,
        alert_name=alert.alert_name,
        severity=alert.severity.value,
        status=alert.status,
        raw_payload=alert.raw_payload,
        labels=alert.labels,
        annotations=alert.annotations,
        fired_at=alert.fired_at,
        resolved_at=alert.resolved_at,
        received_at=alert.received_at,
        incident_id=incident_id,
        correlation_id=alert.correlation_id,
    )
