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

Resolved alert with no open incident to resolve:

- INSERT one ``audit_log`` row and NOTHING ELSE. No incident, no alert, no event.

That last branch exists because a ``resolved`` payload used to fall through to the
create path: with no open incident inside the 5-minute dedup window, a resolve
**opened a brand-new incident** in ``open`` and published ``alert.normalized``, so
the watcher requested an investigation plan for an alert that was already over.
This is the common case, not an edge one — an incident's alert resolves minutes or
hours after it opened, which is well outside the dedup window, so almost every
resolve took it.

The receipt is recorded in ``audit_log`` rather than as an ``alerts`` row with a
NULL ``incident_id``. "We received X and did Y" is what an audit log is for, and it
keeps ``alerts`` meaning *alerts that belong to an incident* — an invariant every
future join and bot query would otherwise have to remember the exceptions to. The
audit row's ``entity_id`` is the normalized alert's id, which never became a row:
that is a log referring to an event, not a foreign key, and ``audit_log`` has no FK
to hold it to one.

**Scope of this guard:** it covers only the resolve that matches NOTHING. A resolve
that DOES match an open incident still takes the duplicate path above — attaching,
bumping ``alert_count``, and publishing — which is also wrong (a resolve is not a
new firing, and that counter feeds the watcher's escalation rule). Updating the
alert's own status and resolving the incident are the next two commits; this one
only stops the bad INSERT.

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
from radar_contracts import AlertNormalizedPayload, NormalizedAlert
from radar_database import Alert, AuditLog, Incident, write_outbox_event
from sqlalchemy.ext.asyncio import AsyncSession

from radar_ingestion.deduper import find_open_incident

ALERT_NORMALIZED_EVENT = "alert.normalized"
"""Outbox event type emitted when a new incident is opened."""

WATCHER_TARGET = "watcher-agent"
"""Target service for the ``alert.normalized`` event."""

ALERT_STATUS_RESOLVED = "resolved"
"""Source-reported alert status meaning the condition is no longer firing."""

RESOLVE_IGNORED_AUDIT_EVENT = "ingestion.resolve_ignored"
"""Audit event recorded when a resolved alert matches no open incident."""

INGESTION_ACTOR = "ingestion"
"""``audit_log.actor`` for records this service writes."""


@dataclass(frozen=True)
class PublishResult:
    """Outcome of persisting one alert.

    ``incident_id`` is ``None`` exactly when ``ignored`` is true — the resolved
    alert matched no open incident, so there is no incident to point at.
    """

    incident_id: UUID | None
    deduplicated: bool
    ignored: bool = False


async def persist_alert(
    session: AsyncSession,
    alert: NormalizedAlert,
    *,
    as_of: datetime | None = None,
) -> PublishResult:
    """Attach ``alert`` to an open incident or open a new one, transactionally.

    ``as_of`` is the dedup reference time (defaults to the alert's
    ``received_at``). A firing alert always publishes an ``alert.normalized``
    event, so the watcher sees every one of them. Adds rows but does not commit —
    the caller commits so the incident, alert, and outbox event are one atomic
    unit. Insert order is irrelevant because the FKs are deferrable.

    A ``resolved`` alert matching no open incident is recorded in ``audit_log``
    and otherwise ignored: it opens nothing and wakes nobody.
    """
    reference = as_of if as_of is not None else alert.received_at
    existing = await find_open_incident(
        session, fingerprint=alert.fingerprint, as_of=reference
    )

    if alert.status == ALERT_STATUS_RESOLVED and existing is None:
        # Nothing to resolve. Falling through here is what used to open an
        # incident — in `open`, for an alert already over — and hand the watcher
        # a plan request for it.
        session.add(_resolve_ignored_audit(alert))
        return PublishResult(incident_id=None, deduplicated=False, ignored=True)

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

    The payload is the shared :class:`~radar_contracts.AlertNormalizedPayload` — the
    normalized alert plus ``incident_id`` and ``deduplicated``, the two facts the
    watcher needs and only ingestion knows. It is *constructed*, not hand-assembled
    into a dict: the watcher parses the same model, so producer and consumer cannot
    drift into disagreeing about the shape.

    Both branches publish the same event type. The watcher's idempotency is keyed on
    ``event_id``, and ``deduplicated`` is a property of the alert, not a different
    kind of thing happening.
    """
    payload = AlertNormalizedPayload(
        **alert.model_dump(),
        incident_id=incident_id,
        deduplicated=deduplicated,
    )
    await write_outbox_event(
        session,
        event_type=ALERT_NORMALIZED_EVENT,
        target_service=WATCHER_TARGET,
        payload=payload.model_dump(mode="json"),
        correlation_id=alert.correlation_id,
    )


def _resolve_ignored_audit(alert: NormalizedAlert) -> AuditLog:
    """Build the audit record for a resolved alert that matched no open incident.

    The only trace this alert leaves. It carries the facts an engineer asking "did
    RADAR ever receive the resolve for X?" needs to answer the question — the
    fingerprint that was searched, and the identity of the alert that carried it —
    without an ``alerts`` row existing to hold them.

    ``entity_type`` is ``"alert"`` rather than ``"incident"`` because there is no
    incident: naming one here would mean inventing an id or pointing at an
    unrelated row.
    """
    return AuditLog(
        event_type=RESOLVE_IGNORED_AUDIT_EVENT,
        entity_type="alert",
        entity_id=alert.id,
        correlation_id=alert.correlation_id,
        actor=INGESTION_ACTOR,
        payload={
            "source": alert.source,
            "source_alert_id": alert.source_alert_id,
            "fingerprint": alert.fingerprint,
            "service_name": alert.service_name,
            "alert_name": alert.alert_name,
            "severity": alert.severity.value,
            "reason": "no_open_incident_for_resolved_alert",
        },
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
