"""Repository CRUD and connection health tests."""

from __future__ import annotations

from uuid import uuid4

from radar_database import Database, Incident, IncidentRepository


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
