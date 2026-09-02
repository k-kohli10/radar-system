"""Transactional incident creation, resolution, and outbox publish.

Ingestion's write path. :func:`persist_alert` branches on FIRING vs RESOLVED, then
does its work in a single transaction the caller commits:

- **Firing, new incident** — INSERT incident + alert + one ``alert.normalized``
  outbox event for the watcher.
- **Firing, duplicate** (open incident matched within the dedup window) — INSERT
  the alert on that incident, bump ``alert_count``/``updated_at``, and INSERT an
  ``alert.normalized`` event **as well**.
- **Resolved, matched a live incident** — flip that incident's still-firing alert
  rows to ``resolved`` at the webhook time, and nothing else: no new alert row, no
  ``alert_count`` bump, no outbox event. A resolve is the firing alert(s)
  transitioning in place, not a new firing, so the watcher (which escalates on
  arrival rate) must not see it and the counter feeding escalation must not move.
- **Resolved, matched nothing** — one ``audit_log`` row and nothing else. This
  branch exists because a ``resolved`` payload used to fall through to the create
  path and open a brand-new incident for an alert already over. The receipt is an
  audit row rather than an ``alerts`` row with a NULL ``incident_id``, so ``alerts``
  keeps meaning *alerts that belong to an incident*; ``entity_id`` is the
  normalized alert's id, which never became a row (``audit_log`` has no FK).

Resolution matching is deliberately NOT dedup's windowed, open-only lookup — see
:func:`resolver.find_resolvable_incident` for why the two must not share a predicate.

**Every firing alert produces an event, duplicates included.** The watcher owns
suppression and escalation policy, and escalation is a claim about arrival rate
("3 alerts within 2 minutes") it cannot enforce against alerts it never sees.
The payload carries the two facts only ingestion knows — ``incident_id`` and
``deduplicated`` — so the event is self-describing.

This module does not commit: it adds rows and the route owns the ``commit()``
boundary, so the incident, alert, and outbox event are one atomic unit (a failure
rolls the whole unit back), duplicate path included. No flush ordering is needed —
the parent->child FKs are deferrable (checked at commit), so the alert may be
written before its incident within the transaction.
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
from radar_ingestion.resolver import (
    ALERT_STATUS_RESOLVED,
    INGESTION_ACTOR,
    find_resolvable_incident,
    mark_incident_alerts_resolved,
    resolve_incident_if_quiet,
)

ALERT_NORMALIZED_EVENT = "alert.normalized"
"""Outbox event type emitted when a new incident is opened."""

WATCHER_TARGET = "watcher-agent"
"""Target service for the ``alert.normalized`` event."""

RESOLVE_IGNORED_AUDIT_EVENT = "ingestion.resolve_ignored"
"""Audit event recorded when a resolved alert matches no open incident."""

INCIDENT_OPENED_AUDIT_EVENT = "ingestion.incident_opened"
"""Audit event recorded when a firing alert opens a new incident."""

ALERT_ATTACHED_AUDIT_EVENT = "ingestion.alert_attached"
"""Audit event recorded when a firing alert deduplicates onto an open incident."""


@dataclass(frozen=True)
class PublishResult:
    """Outcome of persisting one alert. Three disjoint shapes:

    - firing alert that opened or attached to an incident — ``incident_id`` set,
      ``alerts_resolved`` ``None``;
    - resolve that matched a live incident — ``incident_id`` set,
      ``alerts_resolved`` the number of firing alert rows flipped (0 on a duplicate
      delivery), ``incident_resolved`` whether that cleared the last firing alert
      and so transitioned the incident;
    - resolve that matched nothing — ``ignored`` true, ``incident_id`` ``None``.

    ``alerts_resolved is None`` is what distinguishes the firing path from the
    resolve path for the caller.
    """

    incident_id: UUID | None
    deduplicated: bool
    ignored: bool = False
    alerts_resolved: int | None = None
    incident_resolved: bool = False


async def persist_alert(
    session: AsyncSession,
    alert: NormalizedAlert,
    *,
    as_of: datetime | None = None,
) -> PublishResult:
    """Attach a firing alert to an incident, open one, or resolve — transactionally.

    ``as_of`` is the dedup reference time (defaults to the alert's ``received_at``).
    Adds rows but does not commit — the caller commits so each unit is atomic; insert
    order is irrelevant because the FKs are deferrable.

    A ``resolved`` alert takes the resolution path, NOT dedup: resolution matching is
    unwindowed and spans ``{open, investigating}`` (see :mod:`resolver`), because an
    alert usually clears long after the dedup window and while the incident is
    already ``investigating``. A resolve matching no live incident is recorded in
    ``audit_log`` and ignored: it opens nothing and wakes nobody.

    A firing alert always publishes an ``alert.normalized`` event so the watcher sees
    every one.
    """
    if alert.status == ALERT_STATUS_RESOLVED:
        return await _resolve(session, alert)

    reference = as_of if as_of is not None else alert.received_at
    existing = await find_open_incident(
        session, fingerprint=alert.fingerprint, as_of=reference
    )

    if existing is not None:
        existing.alert_count += 1
        existing.updated_at = utcnow()
        session.add(_alert_row(alert, existing.id))
        session.add(_alert_attached_audit(alert, existing.id, existing.alert_count))
        await _publish(session, alert, existing.id, deduplicated=True)
        return PublishResult(incident_id=existing.id, deduplicated=True)

    incident_id = new_id()
    session.add(_new_incident(alert, incident_id))
    session.add(_alert_row(alert, incident_id))
    session.add(_incident_opened_audit(alert, incident_id))
    await _publish(session, alert, incident_id, deduplicated=False)
    return PublishResult(incident_id=incident_id, deduplicated=False)


async def _resolve(session: AsyncSession, alert: NormalizedAlert) -> PublishResult:
    """Handle a ``resolved`` alert: flip firing alerts, resolve the incident if quiet.

    Finds the live incident this ending signal pertains to (``{open,
    investigating}``, unwindowed, under a row lock), flips that incident's still-
    firing alert rows to ``resolved`` at the webhook time, then transitions the
    incident to ``resolved`` — but ONLY if no firing alert remains on it (the gate).
    No ``alert_count`` bump, no ``alert.normalized`` event, no new alert row.

    The alert flips and the incident transition are one transaction (the caller
    commits), so an incident never ends up ``resolved`` while a firing alert of its
    own is still on the table within the same unit of work.

    A resolve matching no live incident is recorded once in ``audit_log`` and
    otherwise ignored. A concurrent duplicate whose incident the winner already
    resolved also lands here: the locked lookup returns ``None`` once the winner
    commits.
    """
    incident = await find_resolvable_incident(session, fingerprint=alert.fingerprint)
    if incident is None:
        session.add(_resolve_ignored_audit(alert))
        return PublishResult(incident_id=None, deduplicated=False, ignored=True)

    # Webhook time: when the source says the condition cleared (Prometheus endsAt);
    # for sources that emit no clearing time, when we received the webhook. Never a
    # fabricated timestamp.
    resolved_at = (
        alert.resolved_at if alert.resolved_at is not None else alert.received_at
    )
    count = await mark_incident_alerts_resolved(
        session, incident_id=incident.id, resolved_at=resolved_at
    )
    incident_resolved = await resolve_incident_if_quiet(
        session, incident_id=incident.id, resolved_at=resolved_at
    )
    return PublishResult(
        incident_id=incident.id,
        deduplicated=False,
        alerts_resolved=count,
        incident_resolved=incident_resolved,
    )


async def _publish(
    session: AsyncSession,
    alert: NormalizedAlert,
    incident_id: UUID,
    *,
    deduplicated: bool,
) -> None:
    """Add the ``alert.normalized`` outbox event for ``alert`` (no commit).

    The payload is the shared :class:`~radar_contracts.AlertNormalizedPayload`,
    *constructed* rather than hand-built as a dict, so producer and consumer cannot
    drift on shape. Both the new-incident and duplicate branches publish the same
    event type; ``deduplicated`` is a property of the alert, not a different kind of
    event, and the watcher's idempotency is keyed on ``event_id``.
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


def _alert_facts(alert: NormalizedAlert) -> dict[str, object | None]:
    """The identifying facts every ingestion audit row carries for an alert."""
    return {
        "source": alert.source,
        "source_alert_id": alert.source_alert_id,
        "alert_id": str(alert.id),
        "fingerprint": alert.fingerprint,
        "service_name": alert.service_name,
        "alert_name": alert.alert_name,
        "severity": alert.severity.value,
    }


def _incident_opened_audit(alert: NormalizedAlert, incident_id: UUID) -> AuditLog:
    """Build the audit record for a firing alert that opened a new incident.

    The incident's birth record: the transition helper only logs *later* status
    changes.
    """
    return AuditLog(
        event_type=INCIDENT_OPENED_AUDIT_EVENT,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=alert.correlation_id,
        actor=INGESTION_ACTOR,
        payload=_alert_facts(alert),
    )


def _alert_attached_audit(
    alert: NormalizedAlert, incident_id: UUID, alert_count: int
) -> AuditLog:
    """Build the audit record for a firing alert deduplicated onto an open incident.

    Records the attach that bumped ``alert_count`` (the counter escalation runs off),
    so the trail shows every alert that joined an incident, not only the first.
    """
    return AuditLog(
        event_type=ALERT_ATTACHED_AUDIT_EVENT,
        entity_type="incident",
        entity_id=incident_id,
        correlation_id=alert.correlation_id,
        actor=INGESTION_ACTOR,
        payload={**_alert_facts(alert), "alert_count": alert_count},
    )


def _resolve_ignored_audit(alert: NormalizedAlert) -> AuditLog:
    """Build the audit record for a resolved alert that matched no open incident.

    The only trace this alert leaves — it carries the fingerprint searched and the
    identity of the alert that carried it, since no ``alerts`` row exists to hold
    them. ``entity_type`` is ``"alert"``, not ``"incident"``: there is no incident to
    name without inventing an id.
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
