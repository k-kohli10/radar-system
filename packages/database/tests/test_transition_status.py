"""``IncidentRepository.transition_status`` against real Postgres.

The transition table is proven pure elsewhere (``test_lifecycle_table``). This
suite is about the behaviour that only real Postgres can show: the row lock that
serialises concurrent transitions, and the transaction boundary that binds a
status change to its audit row. Both are the Phase-3 critical-path bar applied to
the incident lifecycle.

Two guarantees carry the weight:

- **The lock.** ``transition_status`` is a read-modify-write. Without ``SELECT ...
  FOR UPDATE`` two concurrent resolves both read the pre-resolve status, both pass
  validation, and both commit — a lost update and, worse, a DOUBLED audit trail:
  two ``incident.resolved`` rows claiming one resolution happened twice. The
  concurrency test drives exactly that race and asserts one winner, one rejection,
  one audit row.
- **The boundary.** The status change and its audit row are added to the caller's
  transaction, never committed by the method. So a rolled-back transition leaves
  NEITHER — no audit row ever outlives the transition it records. An audit log
  that can contain entries for transitions that were rolled back is not a trustworthy
  audit log; this is what keeps it one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from radar_common import NotFoundError
from radar_database import (
    STATUS_INVESTIGATING,
    STATUS_OPEN,
    STATUS_RESOLVED,
    AuditLog,
    Database,
    Incident,
    IncidentRepository,
    InvalidStateTransitionError,
)
from sqlalchemy import func, select


class _InducedError(Exception):
    """Stands in for any error thrown in the caller between transition and commit."""


async def _seed_incident(db: Database, *, status: str = STATUS_OPEN) -> UUID:
    """Commit one incident in ``status`` and return its id."""
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint="f" * 64,
        service_name="order-service",
        title="order-service OrderFailure",
        severity="critical",
        status=status,
    )
    async with db.session() as session:
        session.add(incident)
        await session.commit()
    return incident.id


async def _load(db: Database, incident_id: UUID) -> Incident:
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    return incident


async def _audit_rows(db: Database) -> list[AuditLog]:
    async with db.session() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return list(rows)


# --- the happy path: status, timestamps, and the audit row commit together ------


async def test_open_to_resolved_sets_status_timestamp_and_audit(db: Database) -> None:
    incident_id = await _seed_incident(db, status=STATUS_OPEN)
    when = datetime(2026, 7, 9, 10, 30, tzinfo=UTC)

    async with db.session() as session:
        repo = IncidentRepository(session)
        result = await repo.transition_status(
            incident_id,
            STATUS_RESOLVED,
            actor="ingestion",
            audit_payload={"resolved_by": "alert_resolution"},
            occurred_at=when,
        )
        await session.commit()
        assert result.status == STATUS_RESOLVED

    incident = await _load(db, incident_id)
    assert incident.status == STATUS_RESOLVED
    assert incident.resolved_at == when
    assert incident.updated_at == when
    # closed_at is reserved — no path in Phase 9 reaches `closed`, and resolving
    # must not touch it.
    assert incident.closed_at is None

    rows = await _audit_rows(db)
    assert len(rows) == 1
    audit = rows[0]
    assert audit.event_type == "incident.resolved"
    assert audit.entity_type == "incident"
    assert audit.entity_id == incident_id
    assert audit.actor == "ingestion"
    assert audit.payload == {"resolved_by": "alert_resolution"}


async def test_open_to_investigating_leaves_resolved_at_null(db: Database) -> None:
    incident_id = await _seed_incident(db, status=STATUS_OPEN)

    async with db.session() as session:
        await IncidentRepository(session).transition_status(
            incident_id, STATUS_INVESTIGATING, actor="feedback-service"
        )
        await session.commit()

    incident = await _load(db, incident_id)
    assert incident.status == STATUS_INVESTIGATING
    assert incident.resolved_at is None
    rows = await _audit_rows(db)
    assert [r.event_type for r in rows] == ["incident.investigating"]


async def test_correlation_id_defaults_to_the_incidents_own(db: Database) -> None:
    """An audit row must carry a correlation id (NOT NULL); default to the
    incident's so a caller that has only the id still produces a valid row."""
    incident_id = await _seed_incident(db, status=STATUS_OPEN)
    incident_corr = (await _load(db, incident_id)).correlation_id

    async with db.session() as session:
        await IncidentRepository(session).transition_status(
            incident_id, STATUS_RESOLVED, actor="ingestion"
        )
        await session.commit()

    rows = await _audit_rows(db)
    assert rows[0].correlation_id == incident_corr


# --- rejection paths: raise, and write nothing ----------------------------------


async def test_invalid_transition_raises_and_writes_nothing(db: Database) -> None:
    """An illegal edge raises InvalidStateTransitionError and leaves no trace.

    Recording the rejected ATTEMPT is the caller's job (its own transaction); the
    executor must not add a half-written audit row. Status is untouched, no audit
    row exists, and the error carries both ends of the rejected edge.
    """
    incident_id = await _seed_incident(db, status=STATUS_RESOLVED)

    async with db.session() as session:
        repo = IncidentRepository(session)
        with pytest.raises(InvalidStateTransitionError) as excinfo:
            # resolved -> investigating is a regression ADR 0016 forbids.
            await repo.transition_status(
                incident_id, STATUS_INVESTIGATING, actor="feedback-service"
            )
        await session.commit()

    assert excinfo.value.from_status == STATUS_RESOLVED
    assert excinfo.value.attempted_status == STATUS_INVESTIGATING
    assert excinfo.value.incident_id == incident_id

    incident = await _load(db, incident_id)
    assert incident.status == STATUS_RESOLVED  # untouched
    assert await _audit_rows(db) == []


async def test_missing_incident_raises_not_found(db: Database) -> None:
    async with db.session() as session:
        repo = IncidentRepository(session)
        with pytest.raises(NotFoundError):
            await repo.transition_status(uuid4(), STATUS_RESOLVED, actor="ingestion")
        await session.commit()

    assert await _audit_rows(db) == []


# --- the boundary: a rolled-back transition leaves NEITHER ----------------------


async def test_rollback_leaves_neither_status_change_nor_audit(db: Database) -> None:
    """Atomicity of the transition and its audit row.

    Mutation that this catches: move the audit write out of the caller's
    transaction (have transition_status open its own session and commit the audit
    row itself). Then the rollback below leaves the audit row behind — an entry
    claiming a resolution that never committed — and the ``== []`` assertion goes
    red. Keeping the write on the caller's session is what binds them.
    """
    incident_id = await _seed_incident(db, status=STATUS_OPEN)

    with pytest.raises(_InducedError):
        async with db.session() as session:
            repo = IncidentRepository(session)
            await repo.transition_status(
                incident_id, STATUS_RESOLVED, actor="ingestion"
            )
            # The transition and its audit row are flushed to the DB inside this
            # transaction; then the caller fails before commit. The rollback must
            # take BOTH with it.
            await session.flush()
            raise _InducedError

    # The audit-row assertion leads, because the trustworthiness of the log is the
    # guarantee: a premature commit inside transition_status reddens THIS line
    # rather than being shadowed by the status assertion below it. No audit row
    # may outlive the transition it records.
    assert await _audit_rows(db) == []  # no orphan audit row
    incident = await _load(db, incident_id)
    assert incident.status == STATUS_OPEN  # never moved
    assert incident.resolved_at is None


# --- the lock: concurrent resolves converge on ONE resolution -------------------


async def test_concurrent_resolves_produce_one_transition_one_audit(
    db: Database,
) -> None:
    """Two resolves race the same incident; FOR UPDATE serialises them.

    This is the load-bearing concurrency guarantee, and it is driven
    DETERMINISTICALLY rather than by a start-barrier — a barrier only lines up the
    two entries, after which the second transaction's UPDATE serialises on the
    first's row write-lock no matter what, so a barrier race passes with OR without
    the read lock and proves nothing (it did: the barrier version stayed green
    under the mutation). The read-modify-write window has to be held open on
    purpose.

    The lever is that ``transition_status`` flushes but does NOT commit — the
    caller owns the boundary. So transaction A can call it and then HOLD, lock and
    all, while B runs against the still-uncommitted row:

    - With ``FOR UPDATE`` (correct): B's ``SELECT ... FOR UPDATE`` blocks on A's
      lock. Only once A commits does B read — and it reads ``resolved``, so its
      ``investigating -> resolved`` edge is now ``resolved -> resolved``, illegal,
      and B is rejected. One transition, one audit row.
    - Without it (the mutation): B's plain SELECT reads the last COMMITTED value,
      ``investigating`` (A hasn't committed), passes validation, and writes its own
      ``incident.resolved`` audit row; its UPDATE then blocks on A's write-lock and
      completes after A commits. Result: B succeeds too — a lost update and TWO
      audit rows. Both assertions below then go red.

    Dropping ``.with_for_update()`` from ``transition_status`` turns this red
    deterministically, which the barrier version did not.
    """
    incident_id = await _seed_incident(db, status=STATUS_INVESTIGATING)

    b_outcome: dict[str, str] = {}

    async def resolve_b() -> None:
        async with db.session() as session:
            try:
                await IncidentRepository(session).transition_status(
                    incident_id, STATUS_RESOLVED, actor="ingestion-b"
                )
                await session.commit()
                b_outcome["result"] = "ok"
            except InvalidStateTransitionError:
                await session.rollback()
                b_outcome["result"] = "rejected"

    async with db.session() as session_a:
        # A resolves and HOLDS: transition_status flushes (taking the row lock and
        # writing the UPDATE) but does not commit, so the lock stays held here.
        await IncidentRepository(session_a).transition_status(
            incident_id, STATUS_RESOLVED, actor="ingestion-a"
        )

        # B now races against A's still-open transaction.
        task_b = asyncio.create_task(resolve_b())
        # Let B reach its SELECT and block (FOR UPDATE) or read-stale-and-block on
        # the write (mutation). Either way it is parked, not finished.
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should be blocked on A's lock, not finished"

        # Release A. B unblocks: re-reads resolved (correct) or completes its
        # doubled write (mutation).
        await session_a.commit()
        await task_b

    # A won. B saw the resolved status A committed and was rejected — not a second
    # successful resolve.
    assert b_outcome["result"] == "rejected"

    incident = await _load(db, incident_id)
    assert incident.status == STATUS_RESOLVED
    assert incident.resolved_at is not None

    # One resolution => one audit row. Two is the doubled trail the lock prevents.
    rows = await _audit_rows(db)
    assert len(rows) == 1
    assert rows[0].event_type == "incident.resolved"


async def test_seeded_incident_count_is_one(db: Database) -> None:
    """Sanity guard on the seed helper: exactly one incident, so the audit-row
    counts above are unambiguous and not masked by a mis-seed."""
    await _seed_incident(db, status=STATUS_OPEN)
    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(Incident))
    assert count == 1
