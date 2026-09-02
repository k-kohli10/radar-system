"""Resolution matching — the alert-level state machine.

Deliberately separate from :mod:`radar_ingestion.deduper`: the two answer different
questions and must not drift into sharing a predicate.

- **Dedup** asks "is this firing alert the same episode?" — about FIRING alerts,
  bounded by the 5-minute window; a second firing outside it is a NEW episode.
- **Resolution** (here) asks "which incident is this ending signal for?" — NO time
  bound. An alert usually clears well past the dedup window, by which time the
  incident is usually ``investigating``, not ``open``, so a windowed open-only
  lookup would miss almost every real resolve.

Resolution therefore matches a same-fingerprint incident in a NON-TERMINAL state
(``open`` or ``investigating``) with no window — ADR 0016 Amendment 2's
``{open, investigating} -> resolved`` authority; see D1 in the Phase 9 notes.

Three operations: find the incident a resolve pertains to (under a row lock), flip
its still-firing alert rows to ``resolved``, and — gated on no firing alert
remaining — transition the incident itself. That gate is ADR 0016's "Multiple
Alerts on One Incident": a resolved incident is a CONSEQUENCE of its alerts
clearing, so it moves only when the LAST firing alert resolves, never the first.

Today every alert on an incident shares one fingerprint, so one resolve clears them
all and the gate always finds zero firing left. The gate is a FORWARD-GUARD for a
future where an incident holds alerts of several conditions; its "hold back while a
firing alert remains" branch is unreachable by today's pipeline and is proven
synthetically.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from radar_database import (
    STATUS_RESOLVED,
    Alert,
    Incident,
    IncidentRepository,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

ALERT_STATUS_FIRING = "firing"
"""Alert-row status for a condition currently asserting (the ``alerts`` default)."""

ALERT_STATUS_RESOLVED = "resolved"
"""Alert-row (and source-reported) status meaning the condition has cleared."""

INGESTION_ACTOR = "ingestion"
"""``audit_log.actor`` / transition actor for records this service writes."""

RESOLVED_BY_ALERT = "alert_resolution"
"""``resolved_by`` on an incident resolved because its alerts cleared (vs an
engineer's Slack action). ADR 0016's audit payload for ingestion-driven resolves."""

# The incident states a resolve may act on: non-terminal, per ADR 0016 Amendment 2.
# `resolved` and `closed` are terminal — a resolve for one is a no-op-match (None).
_RESOLVABLE_INCIDENT_STATES: tuple[str, ...] = ("open", "investigating")


async def find_resolvable_incident(
    session: AsyncSession, *, fingerprint: str
) -> Incident | None:
    """Return the incident a resolve for ``fingerprint`` pertains to, or ``None``.

    Matches a NON-terminal incident (``open`` or ``investigating``) sharing the
    fingerprint, with **no time window** — resolution is not bounded the way dedup
    is. ``None`` means no live incident for this signal, and the caller records the
    resolve as received-and-ignored.

    If more than one non-terminal incident shares the fingerprint the most recently
    opened one wins (D3) — that only happens when an older incident stayed open past
    the dedup window and a newer one opened for the same condition, and the ending
    signal belongs to the newer episode. The stale one is deliberately left open and
    visible rather than over-resolved on evidence that isn't about it.

    The incident is returned under a row lock (``FOR UPDATE``) held for the whole
    read-modify-write (read status, flip alerts, transition if quiet). Two concurrent
    resolves therefore serialise: the loser re-evaluates the ``status IN {open,
    investigating}`` predicate after the winner commits, finds the row now terminal,
    and gets ``None`` — recorded as an ignored duplicate.
    """
    stmt = (
        select(Incident)
        .where(
            Incident.fingerprint == fingerprint,
            Incident.status.in_(_RESOLVABLE_INCIDENT_STATES),
        )
        .order_by(Incident.opened_at.desc())
        .limit(1)
        .with_for_update()
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def mark_incident_alerts_resolved(
    session: AsyncSession, *, incident_id: UUID, resolved_at: datetime
) -> int:
    """Flip this incident's still-firing alert rows to ``resolved`` (no commit).

    Updates every ``alerts`` row for ``incident_id`` whose status is still
    ``firing``, setting ``status = 'resolved'`` and ``resolved_at`` to the webhook
    time. Returns how many rows changed.

    The ``status = 'firing'`` predicate is load-bearing: it doubles as the redelivery
    short-circuit. Alertmanager retries, and on the second delivery every row is
    already ``resolved``, so the UPDATE matches nothing and does NOT re-stamp
    ``resolved_at`` to the later webhook time. Drop the predicate and a duplicate
    resolve moves ``resolved_at`` forward — the mutation the redelivery test guards.

    Adds no new alert row (a resolve is a transition in place) and does not commit —
    the caller owns the transaction boundary, as everywhere in ingestion's write path.
    """
    stmt = (
        update(Alert)
        .where(
            Alert.incident_id == incident_id,
            Alert.status == ALERT_STATUS_FIRING,
        )
        .values(status=ALERT_STATUS_RESOLVED, resolved_at=resolved_at)
        .returning(Alert.id)
        .execution_options(synchronize_session=False)
    )
    flipped = (await session.execute(stmt)).scalars().all()
    return len(flipped)


async def count_firing_alerts(session: AsyncSession, *, incident_id: UUID) -> int:
    """How many alert rows on ``incident_id`` are still ``firing``."""
    count = await session.scalar(
        select(func.count())
        .select_from(Alert)
        .where(
            Alert.incident_id == incident_id,
            Alert.status == ALERT_STATUS_FIRING,
        )
    )
    return int(count or 0)


async def resolve_incident_if_quiet(
    session: AsyncSession, *, incident_id: UUID, resolved_at: datetime
) -> bool:
    """Transition the incident to ``resolved`` iff no firing alert remains.

    THE GATE. If any firing alert row remains it does NOTHING and returns ``False`` —
    the incident is not resolved until its LAST alert clears (ADR 0016, "partial
    resolution does not change incident status"). If none remain it drives
    ``IncidentRepository.transition_status`` to ``resolved``, actor ``ingestion``,
    ``resolved_by: alert_resolution``, stamped with the webhook time.

    The transition reloads the incident under its own ``FOR UPDATE`` and validates the
    edge, so it serialises against a concurrent transition and rejects an illegal one.
    Does not commit — the caller owns the boundary, so the alert flips and the
    incident transition commit as one unit.

    Removing the firing-count guard is the mutation the synthetic gate test catches:
    it would produce a resolved incident that still contains a firing alert.
    """
    if await count_firing_alerts(session, incident_id=incident_id) > 0:
        return False
    await IncidentRepository(session).transition_status(
        incident_id,
        STATUS_RESOLVED,
        actor=INGESTION_ACTOR,
        audit_payload={"resolved_by": RESOLVED_BY_ALERT},
        occurred_at=resolved_at,
    )
    return True
