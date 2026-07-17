"""Repository CRUD and connection health tests."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from radar_database import (
    Database,
    Incident,
    IncidentRepository,
    InvestigationPlan,
    Recommendation,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


def _incident() -> Incident:
    return Incident(
        correlation_id=uuid4(),
        fingerprint="fp",
        service_name="order-service",
        title="Orders failing",
        severity="critical",
    )


async def test_add_applies_server_defaults(db: Database) -> None:
    async with db.session() as session:
        incident = await IncidentRepository(session).add(_incident())
        await session.commit()
        assert incident.id is not None
        assert incident.status == "open"  # server default
        assert incident.alert_count == 1
        assert incident.opened_at is not None


async def test_get_and_list_roundtrip(db: Database) -> None:
    async with db.session() as session:
        repo = IncidentRepository(session)
        created = await repo.add(_incident())
        await repo.add(_incident())
        await session.commit()

        fetched = await repo.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id

        rows = await repo.list(limit=10)
        assert len(rows) == 2


async def test_get_missing_returns_none(db: Database) -> None:
    async with db.session() as session:
        assert await IncidentRepository(session).get(uuid4()) is None


async def test_ping(db: Database) -> None:
    assert await db.ping() is True


# --- one recommendation per incident (idx_rec_one_per_incident) ----------------


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


async def test_an_incident_can_only_have_one_recommendation(db: Database) -> None:
    """The schema refuses a second RCA for one incident. Proven, not read off the DDL.

    Two recommendations means the engineer gets two contradictory Slack cards for one
    incident and cannot tell which is current — and, because the reasoner is the only
    stage that spends money, the redelivery that produced the duplicate also charged
    OpenAI twice.

    The reasoner's dispatch timeout is ordered against its LLM budget so that race
    cannot happen (radar_common.timeouts). This index is what holds when it happens
    anyway: a crash mid-call, a requeued dead letter, an operator replaying an event.
    The guarantee then rests on the schema rather than on two numbers staying in the
    right order forever.
    """
    async with db.session() as session:
        incident = Incident(
            id=uuid4(),
            correlation_id=uuid4(),
            fingerprint="f" * 64,
            service_name="order-service",
            title="order-service OrderProcessingFailureRate",
            severity="high",
        )
        plan = InvestigationPlan(
            id=uuid4(),
            incident_id=incident.id,
            correlation_id=incident.correlation_id,
            steps=[{"order": 1, "description": "check deploys"}],
        )
        session.add_all([incident, plan])
        session.add(_recommendation(incident.id, plan.id))
        await session.commit()

    async with db.session() as session:
        # A SECOND recommendation for the same incident — a different RCA, a different
        # provider, everything different except the incident it is about.
        session.add(
            _recommendation(
                incident.id,
                plan.id,
                root_cause="something else entirely",
                confidence="low",
                is_fallback=True,
                llm_provider="none",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    # The first one survives, unchanged. The duplicate never existed.
    async with db.session() as session:
        rows = (await session.execute(select(Recommendation))).scalars().all()
    assert len(rows) == 1
    assert rows[0].root_cause == "a bad deploy"
    assert rows[0].is_fallback is False


async def test_two_incidents_may_each_have_their_own_recommendation(
    db: Database,
) -> None:
    """The index is per incident, not global — or the second incident gets no RCA."""
    async with db.session() as session:
        for _ in range(2):
            incident = Incident(
                id=uuid4(),
                correlation_id=uuid4(),
                fingerprint="f" * 64,
                service_name="order-service",
                title="t",
                severity="high",
            )
            plan = InvestigationPlan(
                id=uuid4(),
                incident_id=incident.id,
                correlation_id=incident.correlation_id,
                steps=[{"order": 1, "description": "x"}],
            )
            session.add_all([incident, plan, _recommendation(incident.id, plan.id)])
        await session.commit()

    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(Recommendation))
    assert count == 2
