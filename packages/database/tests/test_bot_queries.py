"""Read queries backing the @radar bot: they return the right rows, and the caps bound.

Ordinary read-path tests against real Postgres — do the queries select, filter, order,
and count the right rows. No mutation-proving a SELECT; these are reads, and none of the
methods under test writes anything.

The ONE behaviour with a real failure mode is the cap. ``list_active`` and ``recent``
take a ``limit`` (the bot's ``bot_max_rows``), and a request for more rows than the cap
must return the CAP, not the whole table — otherwise a busy incident feed dumps every
row into Slack. Those two tests seed MORE than the limit and assert exactly the limit
comes back; drop the ``LIMIT`` and they go red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from radar_database import (
    Database,
    Feedback,
    FeedbackRepository,
    Incident,
    IncidentRepository,
    InvestigationPlan,
    OutboxEvent,
    OutboxEventRepository,
    Recommendation,
    RecommendationRepository,
)

_BASE = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _incident(
    *,
    service: str = "order-service",
    status: str = "open",
    opened_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> Incident:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "correlation_id": uuid4(),
        "fingerprint": "f" * 64,
        "service_name": service,
        "title": "t",
        "severity": "high",
        "status": status,
    }
    if opened_at is not None:
        fields["opened_at"] = opened_at
    if resolved_at is not None:
        fields["resolved_at"] = resolved_at
    return Incident(**fields)


async def _seed(db: Database, *rows: Any) -> None:
    async with db.session() as session:
        session.add_all(rows)
        await session.commit()


# --- count_active / list_active: the live set --------------------------------------


async def test_count_active_counts_open_and_investigating_only(db: Database) -> None:
    """The bot's "open" is the live set: open + investigating, never resolved/closed."""
    await _seed(
        db,
        _incident(status="open"),
        _incident(status="investigating"),
        _incident(status="resolved"),
        _incident(status="closed"),
    )
    async with db.session() as session:
        assert await IncidentRepository(session).count_active() == 2


async def test_list_active_returns_live_incidents_newest_first(db: Database) -> None:
    await _seed(
        db,
        _incident(status="open", opened_at=_BASE),
        _incident(status="investigating", opened_at=_BASE + timedelta(minutes=5)),
        _incident(status="resolved", opened_at=_BASE + timedelta(minutes=10)),
    )
    async with db.session() as session:
        rows = await IncidentRepository(session).list_active(limit=10)
    assert [r.status for r in rows] == ["investigating", "open"]  # resolved excluded


async def test_list_active_is_bounded_by_the_limit(db: Database) -> None:
    """The cap, at the query layer: five live incidents, a limit of three -> three rows,
    not five. This is the bounded-authority guarantee — without the LIMIT the query
    returns the whole table and this assertion turns red."""
    await _seed(db, *(_incident(status="open") for _ in range(5)))
    async with db.session() as session:
        rows = await IncidentRepository(session).list_active(limit=3)
    assert len(rows) == 3


# --- recent: last N, optional service filter, capped -------------------------------


async def test_recent_returns_newest_first_across_statuses(db: Database) -> None:
    await _seed(
        db,
        _incident(status="resolved", opened_at=_BASE),
        _incident(status="open", opened_at=_BASE + timedelta(minutes=1)),
    )
    async with db.session() as session:
        rows = await IncidentRepository(session).recent(limit=10)
    # newest first, and resolved incidents are included (unlike list_active)
    assert [r.status for r in rows] == ["open", "resolved"]


async def test_recent_filters_by_service(db: Database) -> None:
    await _seed(
        db,
        _incident(service="order-service"),
        _incident(service="payments"),
        _incident(service="order-service"),
    )
    async with db.session() as session:
        rows = await IncidentRepository(session).recent(limit=10, service="payments")
    assert len(rows) == 1
    assert rows[0].service_name == "payments"


async def test_recent_is_bounded_by_the_limit(db: Database) -> None:
    """The same cap on `last <n>`: four incidents, limit two -> two rows, not four."""
    await _seed(db, *(_incident() for _ in range(4)))
    async with db.session() as session:
        rows = await IncidentRepository(session).recent(limit=2)
    assert len(rows) == 2


# --- summary windows: half-open [start, end) ---------------------------------------


async def test_count_opened_between_is_half_open(db: Database) -> None:
    """[start, end): the start instant counts, the end instant does not — so adjacent
    days never double-count an incident opened exactly at midnight."""
    start, end = _BASE, _BASE + timedelta(hours=1)
    await _seed(
        db,
        _incident(opened_at=start - timedelta(seconds=1)),  # before window
        _incident(opened_at=start),  # inclusive start
        _incident(opened_at=end - timedelta(seconds=1)),  # inside
        _incident(opened_at=end),  # exclusive end
    )
    async with db.session() as session:
        assert await IncidentRepository(session).count_opened_between(start, end) == 2


async def test_count_resolved_between_counts_resolved_in_window(db: Database) -> None:
    start, end = _BASE, _BASE + timedelta(hours=1)
    await _seed(
        db,
        _incident(status="resolved", resolved_at=start + timedelta(minutes=1)),
        _incident(status="resolved", resolved_at=end + timedelta(minutes=1)),  # after
        _incident(status="open"),  # never resolved -> resolved_at NULL, excluded
    )
    async with db.session() as session:
        assert await IncidentRepository(session).count_resolved_between(start, end) == 1


# --- recommendation reads: detail + last-RCA time ----------------------------------


def _recommendation(incident_id: UUID, plan_id: UUID, **over: Any) -> Recommendation:
    fields: dict[str, Any] = {
        "incident_id": incident_id,
        "plan_id": plan_id,
        "correlation_id": uuid4(),
        "llm_provider": "openai",
        "model_alias": "extended",
        "model_id": "gpt-4o",
        "root_cause": "a bad deploy",
        "confidence": "high",
        "recommended_actions": [{"order": 1, "action": "roll back"}],
    }
    fields.update(over)
    return Recommendation(**fields)


async def _seed_incident_with_rca(db: Database) -> tuple[UUID, UUID]:
    """Seed an incident + plan + its RCA; return ``(incident_id, recommendation_id)``
    so a feedback row can reference the real recommendation (the FK is checked at
    commit)."""
    incident = _incident()
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=incident.correlation_id,
        steps=[{"order": 1, "description": "x"}],
    )
    rec = _recommendation(incident.id, plan.id, id=uuid4())
    await _seed(db, incident, plan, rec)
    return incident.id, rec.id


async def test_latest_for_incident_returns_the_rca(db: Database) -> None:
    incident_id, _ = await _seed_incident_with_rca(db)
    async with db.session() as session:
        rec = await RecommendationRepository(session).latest_for_incident(incident_id)
    assert rec is not None
    assert rec.incident_id == incident_id


async def test_latest_for_incident_is_none_without_an_rca(db: Database) -> None:
    await _seed(db, _incident())
    async with db.session() as session:
        assert (
            await RecommendationRepository(session).latest_for_incident(uuid4()) is None
        )


async def test_latest_created_at_is_none_when_empty(db: Database) -> None:
    async with db.session() as session:
        assert await RecommendationRepository(session).latest_created_at() is None


async def test_latest_created_at_returns_the_most_recent(db: Database) -> None:
    await _seed_incident_with_rca(db)
    async with db.session() as session:
        assert await RecommendationRepository(session).latest_created_at() is not None


# --- outbox depth ------------------------------------------------------------------


def _outbox(status: str) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid4(),
        event_type="alert.normalized",
        target_service="watcher",
        payload={},
        correlation_id=uuid4(),
        status=status,
    )


async def test_count_pending_counts_only_pending(db: Database) -> None:
    await _seed(db, _outbox("pending"), _outbox("pending"), _outbox("dispatched"))
    async with db.session() as session:
        assert await OutboxEventRepository(session).count_pending() == 2


# --- feedback ratio for summary ----------------------------------------------------


def _feedback(
    sentiment: str, created_at: datetime, incident_id: UUID, recommendation_id: UUID
) -> Feedback:
    return Feedback(
        recommendation_id=recommendation_id,
        incident_id=incident_id,
        correlation_id=uuid4(),
        sentiment=sentiment,
        llm_provider="openai",
        model_alias="extended",
        created_at=created_at,
    )


async def test_count_by_sentiment_between_groups_in_window(db: Database) -> None:
    start, end = _BASE, _BASE + timedelta(hours=1)
    incident_id, rec_id = await _seed_incident_with_rca(db)
    await _seed(
        db,
        _feedback("helpful", start + timedelta(minutes=1), incident_id, rec_id),
        _feedback("helpful", start + timedelta(minutes=2), incident_id, rec_id),
        _feedback("not_helpful", start + timedelta(minutes=3), incident_id, rec_id),
        _feedback("helpful", end + timedelta(minutes=1), incident_id, rec_id),  # out
    )
    async with db.session() as session:
        repo = FeedbackRepository(session)
        counts = await repo.count_by_sentiment_between(start, end)
    assert counts == {"helpful": 2, "not_helpful": 1}
