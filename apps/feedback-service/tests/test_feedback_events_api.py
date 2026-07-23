"""The feedback-service ``POST /events`` contract and the probe guarding it.

Driven through the real app and the real lifespan against real Postgres, so Vault
secret loading, readiness gating, auth ordering, and the ``processed_events`` gate
are exercised the way a pod exercises them.

The load-bearing test here is the one for the service's OWN event. This is a
skeleton: the delivery handler does not exist yet, so a ``recommendation.created``
must be answered **503 and left unmarked** — retryable, so the outbox keeps it for
redelivery once delivery lands. The wrong choices would silently lose an RCA:
mark-and-drop (a ``processed_events`` row asserting the card was delivered when it
never was) or 422 (immediate dead-letter instead of retry). Both are proven
against, mutation-style, in ``test_own_event_is_left_deliverable``.

**The Slack bot token is required to go ready.** A feedback-service that cannot
reach Slack cannot do its job, so a missing token holds ``/readyz`` at 503 rather
than letting the service accept traffic it can only drop at the first incident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_database import Database, ProcessedEvent
from radar_feedback_service.config import SERVICE_NAME
from radar_feedback_service.main import create_app
from radar_feedback_service.routes import RECOMMENDATION_CREATED_EVENT
from sqlalchemy import func, select

TOKEN = "f" * 64
WRONG_TOKEN = "x" * 64
UNREACHABLE_DSN = "postgresql+asyncpg://radar:radar@127.0.0.1:1/radar"
"""Port 1: nothing listens there, so the ping fails fast rather than hanging."""


def _secrets(
    tmp_path: Path,
    *,
    dsn: str,
    token: str | None = TOKEN,
    slack_token: str | None = "xoxb-test",
) -> Path:
    (tmp_path / "postgres_dsn").write_text(dsn)
    if token is not None:
        (tmp_path / "agent_token").write_text(token)
    if slack_token is not None:
        (tmp_path / "slack_bot_token").write_text(slack_token)
    return tmp_path


@pytest.fixture
def app_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a feedback-service app against a given secrets directory.

    Each app gets its OWN metrics registry: the platform request metrics would
    collide on the global one if two apps were built in one test process.
    """

    def build(secrets_dir: Path) -> Any:
        monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets_dir))
        return create_app(metrics_registry=CollectorRegistry(), with_tracing=False)

    yield build


@pytest_asyncio.fixture
async def client(
    db: Database, database_url: str, tmp_path: Path, app_factory: Any
) -> AsyncIterator[httpx.AsyncClient]:
    app = app_factory(_secrets(tmp_path, dsn=database_url))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://feedback"
        ) as http:
            yield http


def _envelope(event_type: str) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "correlation_id": str(uuid4()),
        "payload": {"incident_id": str(uuid4()), "recommendation_id": str(uuid4())},
    }


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --- probes ----------------------------------------------------------------------


async def test_healthz_is_liveness_only(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_ok_when_secrets_and_db_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_metrics_endpoint_serves(client: httpx.AsyncClient) -> None:
    # A request must complete first: the middleware labels the request metrics with
    # the service name only once it records one, so a fresh registry has the metric
    # families but no service-labelled sample yet.
    await client.get("/healthz")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "radar_requests_total" in response.text
    assert 'service="feedback-service"' in response.text


async def test_readyz_503_when_slack_token_missing(
    db: Database, database_url: str, tmp_path: Path, app_factory: Any
) -> None:
    """The Slack bot token is required to become ready.

    Without it the service cannot post cards, so it must refuse traffic rather
    than accept an incident it can only drop. A skeleton that went ready without
    the token would look healthy and silently deliver nothing.
    """
    app = app_factory(_secrets(tmp_path, dsn=database_url, slack_token=None))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://feedback"
        ) as http:
            response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


async def test_readyz_503_when_database_unreachable(
    db: Database, tmp_path: Path, app_factory: Any
) -> None:
    """Secrets loaded but the database is down -> 503, not a trusted boot state."""
    app = app_factory(_secrets(tmp_path, dsn=UNREACHABLE_DSN))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://feedback"
        ) as http:
            response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["reason"] == "database unreachable"


# --- auth ------------------------------------------------------------------------


async def test_events_requires_token(client: httpx.AsyncClient) -> None:
    response = await client.post("/events", json=_envelope("recommendation.created"))
    assert response.status_code == 401


async def test_events_rejects_wrong_token(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/events",
        json=_envelope("recommendation.created"),
        headers={AGENT_TOKEN_HEADER: WRONG_TOKEN},
    )
    assert response.status_code == 401


async def test_401_beats_422_on_unparseable_body(client: httpx.AsyncClient) -> None:
    """A JSON parse failure with no token is 401, not 422.

    Body decoding happens before dependencies for valid JSON, but a *parse* failure
    is the one case that would answer 422 without the shared guard — telling an
    unauthenticated caller the server read their body. 401 must win.
    """
    response = await client.post(
        "/events",
        content=b"{ not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


# --- the gate and the skeleton's deliberate 503 ----------------------------------


async def test_unhandled_event_type_is_marked_and_dropped(
    client: httpx.AsyncClient, db: Database
) -> None:
    """An event belonging to another service: mark seen, drop, 200.

    Nobody else will deliver it here, and an error would have the worker retry it
    forever — so it is consumed exactly once.
    """
    response = await client.post(
        "/events",
        json=_envelope("incident.plan_requested"),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert await _count(db, ProcessedEvent) == 1


async def test_own_event_is_left_deliverable(
    client: httpx.AsyncClient, db: Database
) -> None:
    """recommendation.created -> 503 and NO marker: the event must stay deliverable.

    This is the load-bearing skeleton guarantee. The delivery handler does not
    exist yet, so the RCA must NOT be consumed. Two mutations this catches:

    - mark-and-drop it (like an unhandled type) -> a processed_events row would
      assert the card was delivered when it never was, and the RCA is lost. This
      asserts ZERO markers.
    - answer 422 instead of 503 -> permanent, so the worker dead-letters it
      immediately instead of retrying once delivery lands. This asserts 503.
    """
    response = await client.post(
        "/events",
        json=_envelope(RECOMMENDATION_CREATED_EVENT),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 503
    # Nothing consumed: no processed_events marker, so redelivery still reaches it.
    assert await _count(db, ProcessedEvent) == 0


async def test_already_processed_is_a_noop(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A redelivery of an already-handled event short-circuits to 200.

    Seed the marker directly, then deliver the same event id: the gate answers
    before any interpretation, so even the service's own event type is a no-op.
    """
    envelope = _envelope("incident.plan_requested")
    async with db.session() as session:
        session.add(
            ProcessedEvent(event_id=envelope["event_id"], processed_by=SERVICE_NAME)
        )
        await session.commit()

    response = await client.post(
        "/events", json=envelope, headers={AGENT_TOKEN_HEADER: TOKEN}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "already_processed"
    # Still exactly one marker — the redelivery wrote nothing.
    assert await _count(db, ProcessedEvent) == 1
