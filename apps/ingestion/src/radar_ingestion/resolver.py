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

This module owns two operations and no policy about the incident itself: finding
the incident a resolve pertains to, and flipping that incident's still-firing alert
rows to ``resolved``. Whether (and when) the INCIDENT then transitions to
``resolved`` is the next commit's gate — a resolved incident is a CONSEQUENCE of
its alerts resolving, decided separately.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from radar_database import Alert, Incident
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

ALERT_STATUS_FIRING = "firing"
"""Alert-row status for a condition currently asserting (the ``alerts`` default)."""

ALERT_STATUS_RESOLVED = "resolved"
"""Alert-row (and source-reported) status meaning the condition has cleared."""

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
    """
    stmt = (
        select(Incident)
        .where(
            Incident.fingerprint == fingerprint,
            Incident.status.in_(_RESOLVABLE_INCIDENT_STATES),
        )
        .order_by(Incident.opened_at.desc())
        .limit(1)
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
