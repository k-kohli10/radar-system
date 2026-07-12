"""Fingerprint deduplication.

Before opening a new incident, ingestion checks whether a matching one is
already open and recent: an open incident with the same fingerprint whose
``opened_at`` falls within the dedup window ending at the incoming alert's
reference time. If one is found the alert attaches to it and no new outbox event
is written; otherwise a new incident is opened. This module owns only the
*query* — the transactional attach-or-create is the publisher's job.

The window is a boundary, not a rounding. With the 5-minute window and an
incident opened at ``T0``, an alert whose reference time is ``T0 + 4m59s``
attaches (``opened_at`` is within the window) while one at ``T0 + 5m01s`` opens a
new incident. The comparison is ``opened_at >= as_of - window``, so exactly
``T0 + 5m00s`` still attaches.

The window is measured from ``opened_at`` (a fixed window from when the incident
opened), matching the plan's "within 5 minutes" and the boundary test framing. A
sliding window from last activity would instead compare against ``updated_at``.

``as_of`` is passed in (the incoming alert's ``received_at``) rather than read
from the clock here, so dedup timing is explicit and deterministically testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from radar_database import Incident
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

INCIDENT_STATUS_OPEN = "open"
"""Incident lifecycle status an alert may deduplicate onto."""

DEDUP_WINDOW = timedelta(minutes=5)
"""How recently an incident must have opened to absorb a duplicate alert."""


async def find_open_incident(
    session: AsyncSession,
    *,
    fingerprint: str,
    as_of: datetime,
    window: timedelta = DEDUP_WINDOW,
) -> Incident | None:
    """Return the open incident this alert deduplicates onto, or ``None``.

    Matches an incident that is ``open``, shares ``fingerprint``, and was opened
    no earlier than ``as_of - window``. If several qualify (which should not
    happen in normal operation) the most recently opened one wins.
    """
    cutoff = as_of - window
    stmt = (
        select(Incident)
        .where(
            Incident.fingerprint == fingerprint,
            Incident.status == INCIDENT_STATUS_OPEN,
            Incident.opened_at >= cutoff,
        )
        .order_by(Incident.opened_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
