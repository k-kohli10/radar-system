"""Transactional incident creation and outbox publish.

The heart of ingestion's write path. Given a normalized alert, :func:`persist_alert`
either attaches it to an already-open incident or opens a new one — and does so
in a single transaction the caller commits, so the incident, the alert, and the
outbox event are all-or-nothing.

New incident (no open match within the dedup window):

- INSERT incident, INSERT alert (linked to it), and — reusing the shared outbox
  writer — INSERT one ``alert.normalized`` outbox event targeting the watcher
  agent, all in one transaction.

Duplicate (an open incident matched):

- INSERT the alert linked to that incident, bump its ``alert_count`` /
  ``updated_at``, and INSERT an ``alert.normalized`` outbox event **as well**.

**Every alert produces an event, duplicates included.** Ingestion owns incident
identity (it opens the incident and dedups on the fingerprint); the watcher owns
correlation *policy* — suppression and escalation — and it cannot enforce either
against alerts it never sees. Escalation in particular is a statement about
arrival rate ("3 alerts within 2 minutes"), so a watcher fed only first-of-kind
alerts could never observe the condition and the rule would be dead by
construction. Duplicates therefore reach the watcher too, tagged as such.

The payload is the normalized alert plus the two facts only ingestion knows —
``incident_id`` (which incident this alert landed on) and ``deduplicated``
(whether that incident already existed). Carrying them makes the event
self-describing: the watcher branches on ``deduplicated`` rather than inferring
"was this new?" from a correlation-id comparison, and it resolves the incident by
id rather than re-querying the alerts table on the strength of an implicit
guarantee about what this transaction wrote.

This module does not flush for ordering and does not commit: it adds the rows and
the route owns the ``commit()`` boundary, so the incident, alert, and outbox event
are issued and FK-checked as one atomic unit (a failure rolls the whole unit
back). That holds on the duplicate path too — the alert and its event commit
together or not at all. No manual flush ordering is needed: the parent->child FKs
are deferrable (checked at commit), so the alert may be written before its
incident within the transaction.
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
    ``received_at``). Either way an ``alert.normalized`` event is published, so
    the watcher sees every alert. Adds rows but does not commit — the caller
    commits so the incident, alert, and outbox event are one atomic unit. Insert
    order is irrelevant because the FKs are deferrable.
    """
    reference = as_of if as_of is not None else alert.received_at
    existing = await find_open_incident(
        session, fingerprint=alert.fingerprint, as_of=reference
    )

    if existing is not None:
        existing.alert_count += 1
        existing.updated_at = utcnow()
        session.add(_alert_row(alert, existing.id))
        await _publish(session, alert, existing.id, deduplicated=True)
        return PublishResult(incident_id=existing.id, deduplicated=True)

    incident_id = new_id()
    session.add(_new_incident(alert, incident_id))
    session.add(_alert_row(alert, incident_id))
    await _publish(session, alert, incident_id, deduplicated=False)
    return PublishResult(incident_id=incident_id, deduplicated=False)


async def _publish(
    session: AsyncSession,
    alert: NormalizedAlert,
    incident_id: UUID,
    *,
    deduplicated: bool,
) -> None:
    """Add the ``alert.normalized`` outbox event for ``alert`` (no commit).

    The payload is the normalized alert plus ``incident_id`` and
    ``deduplicated`` — the two facts the watcher needs and only ingestion knows.
    Both branches publish the same event type: the watcher's idempotency is keyed
    on ``event_id``, and ``deduplicated`` is a property of the alert, not a
    different kind of thing happening.
    """
    payload = alert.model_dump(mode="json")
    payload["incident_id"] = str(incident_id)
    payload["deduplicated"] = deduplicated
    await write_outbox_event(
        session,
        event_type=ALERT_NORMALIZED_EVENT,
        target_service=WATCHER_TARGET,
        payload=payload,
        correlation_id=alert.correlation_id,
    )


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
