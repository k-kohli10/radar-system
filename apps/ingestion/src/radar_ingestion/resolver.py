"""Resolution matching — the alert-level state machine.

Deliberately separate from :mod:`radar_ingestion.deduper`, because dedup and
resolution answer two different questions and must not drift into sharing a
predicate:

- **Dedup** (deduper) asks "is this firing alert a recurrence or the same
  episode?" — a question about FIRING alerts, bounded by the 5-minute window. A
  second firing outside the window is a NEW episode.
- **Resolution** (here) asks "which incident is this ending signal for?" — no time
  bound at all. An alert usually clears minutes or hours after it opened, well past
  the dedup window, so a windowed lookup would miss almost every real resolve. And
  by the time it clears the incident is usually ``investigating`` (the RCA card was
  sent), not ``open``.

So resolution matches an incident that shares the fingerprint and is in a
NON-TERMINAL state — ``open`` or ``investigating``, exactly ADR 0016 Amendment 2's
``{open, investigating} -> resolved`` authority — with NO window. See D1 in the
Phase 9 working notes.

This module owns three operations: finding the incident a resolve pertains to
(under a row lock), flipping that incident's still-firing alert rows to
``resolved``, and — gated on no firing alert remaining — transitioning the incident
itself. That last step is the KEY point of ADR 0016's "Multiple Alerts on One
Incident": a resolved incident is a CONSEQUENCE of its alerts clearing, so the
incident moves only when the LAST firing alert on it resolves, never on the first.

Today every alert on an incident shares one fingerprint (dedup attaches only
same-fingerprint alerts and the incident's fingerprint is frozen at the first),
so a single resolve clears them all and the gate always finds zero firing left —
the incident resolves on that one resolve. The gate is therefore a FORWARD-GUARD:
it earns its keep only in a future where an incident could hold alerts of several
conditions, where a resolve for one must not resolve the incident while another
still fires. Its "hold back while a firing alert remains" branch is unreachable by
today's pipeline and is proven synthetically; its "resolve when none remain" branch
is the live path.
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
    opened one wins (D3). This should not happen in normal operation: dedup keeps
    one open incident per fingerprint within the window, so a second only appears
    when an older incident stayed open PAST the window and a newer one opened for
    the same condition. When it does, the older incident is effectively stale —
    abandoned past its window — and resolving the NEWEST matches the ending signal
    to the episode it belongs to. The stale one is deliberately left open and
    visible, so a lingering open incident is the symptom rather than a mystery; over-
    resolving would both close an incident on evidence that isn't about it and erase
    that signal.

    The incident is returned under a row lock (``FOR UPDATE``). Resolving is a
    read-modify-write on the incident — read status, flip alerts, transition if
    quiet — so the whole path holds the lock: two concurrent resolves for the same
    incident serialise, and the loser re-evaluates the ``status IN {open,
    investigating}`` predicate after the winner commits. Because the winner moved the
    incident to ``resolved`` (terminal), the row no longer matches and the loser gets
    ``None`` — recorded as an ignored duplicate rather than fighting to resolve an
    incident that already is. (This closes concurrent DUPLICATE resolves; a firing
    webhook racing a resolve is discussed in the commit's scope notes.)
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

    The ``status = 'firing'`` predicate is load-bearing and does double duty as the
    redelivery short-circuit: Alertmanager retries, so the same resolve arrives more
    than once. On the second delivery every row is already ``resolved``, so the
    UPDATE matches nothing, changes nothing, and — crucially — does NOT re-stamp
    ``resolved_at`` to the later webhook time. Idempotency falls out of the predicate;
    there is no separate "have we seen this?" bookkeeping. Drop the predicate and a
    duplicate resolve moves ``resolved_at`` forward — the mutation the redelivery test
    guards.

    Adds no new alert row: a resolve is the firing alert(s) transitioning
    ``firing -> resolved``, not a new alert. Does not commit — the caller owns the
    transaction boundary, as everywhere else in ingestion's write path.
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

    THE GATE. Counts the incident's still-firing alert rows; if any remain it does
    NOTHING and returns ``False`` — the incident is not resolved until its LAST alert
    clears (ADR 0016, "partial resolution does not change incident status"). If none
    remain it drives ``IncidentRepository.transition_status`` to ``resolved``, actor
    ``ingestion``, ``resolved_by: alert_resolution``, stamped with the webhook time,
    and returns ``True``.

    The transition reloads the incident under its own ``FOR UPDATE`` and validates
    the edge, so it also serialises against a concurrent transition and rejects an
    illegal one. This method does not commit — the caller owns the boundary, so the
    alert flips and the incident transition commit as one unit.

    Removing the firing-count guard (always transitioning) is the mutation the
    synthetic gate test catches: with a firing alert still present it would resolve
    the incident anyway, producing a resolved incident that still contains a firing
    alert.
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
