"""The feedback-service ``POST /events`` contract and the probe guarding it.

Driven through the real app and the real lifespan against real Postgres, so Vault
secret loading, readiness gating, auth ordering, and the ``processed_events`` gate
are exercised the way a pod exercises them.

These prove the handler WIRES the delivery correctly: the gate, the auth ordering,
the readiness contract, and the status mapping (200 delivered, 422 on a missing
recommendation, 503 on a failed post — each with the right processed_events
outcome). The delivery GUARANTEES themselves — no-double-post, the at-least-once
ordering, the row lock, the UNIQUE — are proven on ``deliver_rca`` directly in
``test_delivery.py``, where they can be driven and mutated in isolation.

**The Slack bot token is required to go ready.** A feedback-service that cannot
reach Slack cannot do its job, so a missing token holds ``/readyz`` at 503 rather
than letting the service accept traffic it can only drop at the first incident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fakes import FakeInteractionSource, FakeNotifier
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_database import (
    Database,
    Incident,
    InvestigationPlan,
    ProcessedEvent,
    Recommendation,
)
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
        # The Socket Mode source is injected as a fake in these tests, so the app-level
        # token is never read; written alongside the bot token only for realism.
        (tmp_path / "slack_app_token").write_text("xapp-test")
    return tmp_path


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def _fake_source(handler: Any, mention_handler: Any) -> FakeInteractionSource:
    """A socketless source factory: ignores both handlers, opens no WebSocket."""
    return FakeInteractionSource()


@pytest.fixture
def app_factory(
    monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier
) -> Iterator[Any]:
    """Build a feedback-service app against a given secrets directory.

    Each app gets its OWN metrics registry: the platform request metrics would
    collide on the global one if two apps were built in one test process. The fake
    notifier is injected so the delivery path never touches a real Slack workspace.
    """

    def build(secrets_dir: Path, *, inject_notifier: bool = True) -> Any:
        monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets_dir))
        return create_app(
            metrics_registry=CollectorRegistry(),
            with_tracing=False,
            notifier_override=notifier if inject_notifier else None,
            # Socketless receive side: no WebSocket to Slack in a test.
            interaction_source_factory=_fake_source,
        )

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


def _envelope(event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "correlation_id": str(uuid4()),
        "payload": payload
        or {"incident_id": str(uuid4()), "recommendation_id": str(uuid4())},
    }


async def _seed_incident_and_recommendation(db: Database) -> tuple[UUID, UUID]:
    """Commit an incident and its recommendation, as the pipeline would leave them.

    Returns ``(incident_id, recommendation_id)``. slack_message_ts starts NULL —
    undelivered — which is what the delivery path expects.
    """
    incident_id = uuid4()
    plan_id = uuid4()
    rec_id = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint="f" * 64,
                service_name="order-service",
                title="order-service OrderFailure",
                severity="high",
                status="open",
            )
        )
        session.add(
            InvestigationPlan(
                id=plan_id,
                incident_id=incident_id,
                correlation_id=uuid4(),
                steps=[{"order": 1, "description": "check deploys"}],
            )
        )
        session.add(
            Recommendation(
                id=rec_id,
                incident_id=incident_id,
                plan_id=plan_id,
                correlation_id=uuid4(),
                llm_provider="openai",
                model_alias="extended",
                model_id="gpt-4o",
                root_cause="A bad deploy raised the pool timeout.",
                confidence="high",
                recommended_actions=[{"order": 1, "action": "Roll back."}],
            )
        )
        await session.commit()
    return incident_id, rec_id


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
    # inject_notifier=False exercises the real construction path, which reads the
    # bot-token file — the token whose absence must hold readiness at 503.
    app = app_factory(
        _secrets(tmp_path, dsn=database_url, slack_token=None), inject_notifier=False
    )
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


# --- interaction source wiring ---------------------------------------------------


async def test_lifespan_starts_and_closes_the_interaction_source(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receive side is wired into the lifespan: the source is started on the way in
    (so clicks are being received once the pod is ready) and closed on the way out (so
    the socket stops before the database pool is disposed)."""
    sources: list[FakeInteractionSource] = []

    def factory(handler: Any, mention_handler: Any) -> FakeInteractionSource:
        source = FakeInteractionSource()
        sources.append(source)
        return source

    monkeypatch.setenv("RADAR_SECRETS_DIR", str(_secrets(tmp_path, dsn=database_url)))
    app = create_app(
        metrics_registry=CollectorRegistry(),
        with_tracing=False,
        notifier_override=FakeNotifier(),
        interaction_source_factory=factory,
    )
    async with app.router.lifespan_context(app):
        assert len(sources) == 1
        assert sources[0].started is True
        assert sources[0].closed is False
    assert sources[0].closed is True


async def test_readyz_503_when_interaction_source_fails_to_start(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket that will not open holds readiness at 503, not a card whose buttons
    reach nothing. The source is started inside the try and BEFORE mark_ready, so a
    failing start leaves the pod out of rotation rather than advertising a half-deaf
    service — the teeth of that placement."""

    class _BoomSource:
        async def start(self) -> None:
            raise RuntimeError("socket refused")

        async def close(self) -> None:
            pass

    monkeypatch.setenv("RADAR_SECRETS_DIR", str(_secrets(tmp_path, dsn=database_url)))
    app = create_app(
        metrics_registry=CollectorRegistry(),
        with_tracing=False,
        notifier_override=FakeNotifier(),
        interaction_source_factory=lambda handler, mention_handler: _BoomSource(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://feedback"
        ) as http:
            response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


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


# --- the gate and the delivery-path HTTP contract --------------------------------
#
# These prove the handler WIRES the delivery correctly (gate, status mapping). The
# delivery guarantees themselves — no-double-post, at-least-once ordering, the lock,
# the UNIQUE — are proven on deliver_rca directly in test_delivery.py.


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


async def test_recommendation_created_delivers_and_marks_processed(
    client: httpx.AsyncClient, db: Database, notifier: FakeNotifier
) -> None:
    """The happy path end to end: post one card, record ts + marker, 200 delivered."""
    incident_id, rec_id = await _seed_incident_and_recommendation(db)
    response = await client.post(
        "/events",
        json=_envelope(
            RECOMMENDATION_CREATED_EVENT,
            {"incident_id": str(incident_id), "recommendation_id": str(rec_id)},
        ),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "delivered"

    # Exactly one card posted to the configured channel.
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["channel"] == "#all-my-tech"

    # The delivery is recorded: ts on the recommendation and a processed marker.
    async with db.session() as session:
        rec = await session.get(Recommendation, rec_id)
        assert rec is not None
        assert rec.slack_message_ts is not None
    assert await _count(db, ProcessedEvent) == 1


async def test_missing_recommendation_is_422_and_unmarked(
    client: httpx.AsyncClient, db: Database, notifier: FakeNotifier
) -> None:
    """A recommendation the payload names but that does not exist -> 422, no post.

    Permanent (corruption, not a race), so it dead-letters. Nothing posted, nothing
    marked.
    """
    response = await client.post(
        "/events",
        json=_envelope(
            RECOMMENDATION_CREATED_EVENT,
            {"incident_id": str(uuid4()), "recommendation_id": str(uuid4())},
        ),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )
    assert response.status_code == 422
    assert notifier.calls == []
    assert await _count(db, ProcessedEvent) == 0


async def test_post_failure_is_503_and_unmarked(
    db: Database, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing Slack post -> 503, no ts, no marker: the RCA stays deliverable.

    The at-least-once contract at the HTTP boundary. Uses a failing notifier, so the
    post raises, the transaction rolls back, and redelivery will retry.
    """
    incident_id, rec_id = await _seed_incident_and_recommendation(db)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(_secrets(tmp_path, dsn=database_url)))
    app = create_app(
        metrics_registry=CollectorRegistry(),
        with_tracing=False,
        notifier_override=FakeNotifier(fail=True),
        interaction_source_factory=_fake_source,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://feedback"
        ) as http:
            response = await http.post(
                "/events",
                json=_envelope(
                    RECOMMENDATION_CREATED_EVENT,
                    {
                        "incident_id": str(incident_id),
                        "recommendation_id": str(rec_id),
                    },
                ),
                headers={AGENT_TOKEN_HEADER: TOKEN},
            )
    assert response.status_code == 503
    async with db.session() as session:
        rec = await session.get(Recommendation, rec_id)
        assert rec is not None
        assert rec.slack_message_ts is None  # nothing recorded
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
