"""The ``POST /events`` contract, and the readiness probe that guards it.

Driven through the real app and the real lifespan, against real Postgres — so the
Vault-secret loading, the readiness gating, the auth ordering, and the
``processed_events`` gate are all exercised the way a pod exercises them.

Two properties here are the sort that pass by accident and fail in production:

**401 must beat 422 — for UNPARSEABLE JSON specifically.** This is narrower than it
first appears, and the obvious test for it is vacuous. FastAPI resolves route
dependencies before *schema* validation, so valid-JSON-wrong-schema is a 401 whether
or not the guard exists. But a JSON *parse* failure happens during body decoding,
before any dependency runs — so without the guard it answers 422, and an
unauthenticated caller has learned the server read their body. The test therefore
sends a bad token together with genuinely unparseable bytes, which is the only
combination that can distinguish the two orderings.

**/readyz must fail when the database is down, not merely when a secret is missing.**
A probe that checks only its own startup state reports healthy with a dead database
underneath it — Kubernetes keeps routing traffic to a pod that cannot do anything.
So it is asserted with secrets present and the database unreachable, which is exactly
the case a secrets-only probe gets wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import AlertNormalizedPayload, NormalizedAlert, Severity
from radar_database import Database, Incident, ProcessedEvent
from radar_watcher_agent.config import SERVICE_NAME
from radar_watcher_agent.main import create_app
from sqlalchemy import func, select

TOKEN = "w" * 64
WRONG_TOKEN = "x" * 64
UNREACHABLE_DSN = "postgresql+asyncpg://radar:radar@127.0.0.1:1/radar"
"""Port 1: nothing listens there, so the ping fails fast rather than hanging."""


def _secrets(tmp_path: Path, *, dsn: str, token: str | None = TOKEN) -> Path:
    """Write the Vault secret files a watcher pod would be given."""
    (tmp_path / "postgres_dsn").write_text(dsn)
    if token is not None:
        (tmp_path / "agent_token").write_text(token)
    return tmp_path


@pytest.fixture
def app_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """Build a watcher app against a given secrets directory.

    Each app gets its OWN metrics registry: the platform request metrics would
    collide on the global one if two apps were built in a single test process.
    """

    def build(secrets_dir: Path) -> Any:
        monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets_dir))
        return create_app(metrics_registry=CollectorRegistry(), with_tracing=False)

    yield build


@pytest_asyncio.fixture
async def client(
    db: Database, database_url: str, tmp_path: Path, app_factory: Any
) -> AsyncIterator[httpx.AsyncClient]:
    """A client over a fully-ready watcher: secrets present, database live."""
    app = app_factory(_secrets(tmp_path, dsn=database_url))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://watcher"
        ) as http:
            yield http


@pytest_asyncio.fixture
async def incident_id(db: Database) -> UUID:
    """An open incident, as ingestion would have written it.

    The handler now RESOLVES the incident its payload names — an event pointing at an
    incident that does not exist is a 422 (see test_correlation). So the gate and auth
    tests here need a real one to act on.
    """
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint="f" * 64,
        service_name="order-service",
        title="order-service OrderProcessingFailureRate",
        severity="high",
        status="open",
        alert_count=1,
    )
    async with db.session() as session:
        session.add(incident)
        await session.commit()
    return incident.id


def _event(
    event_id: UUID | None = None,
    incident_id: UUID | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A well-formed alert.normalized delivery envelope."""
    alert = NormalizedAlert(
        source="mock",
        fingerprint="f" * 64,
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
        severity=Severity.HIGH,
        raw_payload={},
        fired_at=datetime.now(UTC),
    )
    payload = AlertNormalizedPayload(
        **alert.model_dump(),
        incident_id=incident_id or uuid4(),
        deduplicated=False,
    )
    body: dict[str, Any] = {
        "event_id": str(event_id or uuid4()),
        "event_type": "alert.normalized",
        "correlation_id": str(uuid4()),
        "payload": payload.model_dump(mode="json"),
    }
    body.update(overrides)
    return body


async def _processed_count(db: Database) -> int:
    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(ProcessedEvent.processed_by == SERVICE_NAME)
        )
    return int(count or 0)


# --- authentication ordering -------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"event_id": ', id="truncated-json"),
        pytest.param(b"not json at all", id="garbage"),
    ],
)
async def test_malformed_json_with_bad_token_returns_401_not_422(
    client: httpx.AsyncClient, db: Database, body: bytes
) -> None:
    """The load-bearing case, and the ONLY body that actually exercises the guard.

    The distinction is subtle and worth stating, because getting it wrong produces a
    test that passes for free and proves nothing:

    - **Valid JSON with the wrong schema** is 401 with or without the guard, because
      FastAPI resolves route dependencies *before* schema validation. Asserting on
      that case tests nothing.
    - **Unparseable JSON** fails during body decoding, which happens BEFORE the
      dependency ever runs — so without the guard it is a 422, and an
      unauthenticated caller has just learned that the server got as far as parsing
      their body. That is the leak, and this is the case that catches it.

    Verified by mutation: removing the guard turns exactly these two parameters red
    and leaves every other test green.
    """
    response = await client.post(
        "/events",
        content=body,
        headers={
            AGENT_TOKEN_HEADER: WRONG_TOKEN,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert await _processed_count(db) == 0


async def test_malformed_json_with_no_token_at_all_returns_401(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/events",
        content=b'{"event_id": ',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401


async def test_malformed_json_with_a_valid_token_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """An authenticated caller — and only they — learns the body was malformed.

    The guard must not swallow the 422 for a legitimate caller: the outbox worker
    treats 422 as permanent and dead-letters the event, which is right for a body it
    can never send correctly. Turning that into a 401 would send it chasing a
    credential problem it does not have.
    """
    response = await client.post(
        "/events",
        content=b'{"event_id": ',
        headers={AGENT_TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
    )

    assert response.status_code == 422


async def test_wrong_schema_with_a_valid_token_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """Parseable JSON that is not an envelope is a 422 for an authenticated caller."""
    response = await client.post(
        "/events",
        json={"not": "an envelope"},
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422


async def test_well_formed_event_with_a_bad_token_returns_401(
    client: httpx.AsyncClient, db: Database
) -> None:
    response = await client.post(
        "/events", json=_event(), headers={AGENT_TOKEN_HEADER: WRONG_TOKEN}
    )

    assert response.status_code == 401
    # The event was rejected at the door: no marker, so a later legitimate delivery
    # of the same event still gets processed rather than being skipped as "seen".
    assert await _processed_count(db) == 0


async def test_extra_envelope_field_is_rejected(client: httpx.AsyncClient) -> None:
    """EventEnvelope forbids extras, so a widened body is a 422 for the authenticated.

    This is what keeps the worker's private row bookkeeping from being quietly
    accepted as part of the contract.
    """
    response = await client.post(
        "/events",
        json=_event() | {"attempts": 3},
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 422


# --- the processed_events gate ------------------------------------------------


async def test_event_is_processed_and_marked(
    client: httpx.AsyncClient, db: Database, incident_id: UUID
) -> None:
    event_id = uuid4()

    response = await client.post(
        "/events",
        json=_event(event_id, incident_id),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    async with db.session() as session:
        marker = await session.get(ProcessedEvent, (event_id, SERVICE_NAME))
    assert marker is not None, "the handler must record what it handled"


async def test_redelivery_is_a_no_op(
    client: httpx.AsyncClient, db: Database, incident_id: UUID
) -> None:
    """The gate: the same event delivered twice is handled once.

    The worker delivers at-least-once, so this is not a hypothetical — a dispatch
    that times out after the handler committed is redelivered verbatim. The second
    delivery must return 200 (so the worker stops retrying) while doing no work.
    """
    body = _event(incident_id=incident_id)

    first = await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})
    second = await client.post(
        "/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == {"status": "processed"}
    assert second.json() == {"status": "already_processed"}
    # One marker, not two — and the composite PK would have raised had the handler
    # tried to write a second.
    assert await _processed_count(db) == 1


async def test_unhandled_event_type_is_marked_seen_and_dropped(
    client: httpx.AsyncClient, db: Database
) -> None:
    """An event this agent does not handle must not be redelivered forever.

    Returning an error would have the worker retry it to exhaustion and dead-letter
    it. It is marked processed and dropped, so it is dispatched exactly once.
    """
    response = await client.post(
        "/events",
        json=_event(event_type="incident.reasoning_requested"),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await _processed_count(db) == 1


# --- readiness ----------------------------------------------------------------


async def test_readyz_is_ready_when_secrets_and_database_are_both_good(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readyz_is_503_when_the_database_is_unreachable(
    tmp_path: Path, app_factory: Any
) -> None:
    """Secrets ALL present, database dead — the probe must still say no.

    This is the case a startup-state-only probe gets wrong: it would report ready,
    Kubernetes would route traffic to a pod that cannot touch its database, and the
    failure would surface as errors to callers instead of a pod pulled from service.
    """
    app = app_factory(_secrets(tmp_path, dsn=UNREACHABLE_DSN))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://watcher"
        ) as http:
            response = await http.get("/readyz")
            # Liveness is a different question: the process is fine, so /healthz
            # must NOT fail — or Kubernetes would kill the pod over a DB blip
            # instead of merely taking it out of rotation.
            liveness = await http.get("/healthz")

    assert response.status_code == 503
    assert response.json()["reason"] == "database unreachable"
    assert liveness.status_code == 200


async def test_readyz_is_503_when_a_secret_is_missing(
    tmp_path: Path, app_factory: Any, database_url: str
) -> None:
    """No agent_token in the mount: not ready, and it says which file is missing."""
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://watcher"
        ) as http:
            response = await http.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # Probe-safe: it names the FILE, never a secret value.
    assert "agent_token" in body["reason"]


async def test_events_is_503_before_the_secrets_load(
    tmp_path: Path, app_factory: Any, database_url: str
) -> None:
    """With no token loaded, /events answers 503 — not 401, and not a crash.

    The distinction matters to the worker: 503 is retryable (it backs off and tries
    again once the pod is ready), while 401 is permanent and would dead-letter the
    event outright over what is only a slow start.
    """
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://watcher"
        ) as http:
            response = await http.post(
                "/events", json=_event(), headers={AGENT_TOKEN_HEADER: TOKEN}
            )

    assert response.status_code == 503
