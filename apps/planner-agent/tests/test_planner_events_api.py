"""The planner's ``POST /events`` contract, and the readiness probe guarding it.

Driven through the real app and the real lifespan against real Postgres, so the
Vault-secret loading, the readiness gating, the auth ordering, and the
``processed_events`` gate are exercised the way a pod exercises them.

The auth guard is now the SHARED one (``radar_common.auth``), so these tests are
not redundant with the watcher's — they are the *second consumer* proving the
shared code holds for them too. A guard that works for one service and silently
not another is exactly what an extraction can produce, and only per-service tests
catch it.

**401 must beat 422 — for UNPARSEABLE JSON specifically.** FastAPI resolves route
dependencies before *schema* validation, so valid-JSON-wrong-shape is a 401 for
free and asserting on it proves nothing. A JSON *parse* failure happens during
body decoding, before any dependency runs — so without the guard it answers 422,
and an unauthenticated caller has learned the server read their body. That is the
only case that can distinguish the two orderings.

**/readyz must fail when the database is down**, not merely when a secret is
missing. A probe that trusts its own startup state reports healthy with a dead
database underneath it, and Kubernetes keeps sending it traffic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import PlanRequestedPayload
from radar_database import Database, ProcessedEvent
from radar_planner_agent.config import SERVICE_NAME
from radar_planner_agent.main import create_app
from sqlalchemy import func, select

TOKEN = "p" * 64
WRONG_TOKEN = "x" * 64
UNREACHABLE_DSN = "postgresql+asyncpg://radar:radar@127.0.0.1:1/radar"
"""Port 1: nothing listens there, so the ping fails fast rather than hanging."""


def _secrets(tmp_path: Path, *, dsn: str, token: str | None = TOKEN) -> Path:
    (tmp_path / "postgres_dsn").write_text(dsn)
    if token is not None:
        (tmp_path / "agent_token").write_text(token)
    return tmp_path


@pytest.fixture
def app_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a planner app against a given secrets directory.

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
            transport=transport, base_url="http://planner"
        ) as http:
            yield http


def _event(event_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    """A well-formed plan_requested envelope, built from the real contract."""
    payload = PlanRequestedPayload(
        incident_id=uuid4(),
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
    )
    body: dict[str, Any] = {
        "event_id": str(event_id or uuid4()),
        "event_type": "incident.plan_requested",
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


# --- authentication ordering (through the SHARED guard) ----------------------


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
    response = await client.post(
        "/events",
        content=body,
        headers={AGENT_TOKEN_HEADER: WRONG_TOKEN, "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert await _processed_count(db) == 0


async def test_malformed_json_with_a_valid_token_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """The guard must not swallow the 422 for a legitimate caller.

    The worker treats 422 as permanent and dead-letters — right for a body it can
    never send correctly. Turning it into a 401 would send it chasing a credential
    problem it does not have.
    """
    response = await client.post(
        "/events",
        content=b'{"event_id": ',
        headers={AGENT_TOKEN_HEADER: TOKEN, "Content-Type": "application/json"},
    )

    assert response.status_code == 422


async def test_well_formed_event_with_a_bad_token_returns_401(
    client: httpx.AsyncClient, db: Database
) -> None:
    response = await client.post(
        "/events", json=_event(), headers={AGENT_TOKEN_HEADER: WRONG_TOKEN}
    )

    assert response.status_code == 401
    # Rejected at the door: no marker, so a later legitimate delivery of the same
    # event is still processed rather than skipped as "already seen".
    assert await _processed_count(db) == 0


# --- the processed_events gate ------------------------------------------------


async def test_event_is_processed_and_marked(
    client: httpx.AsyncClient, db: Database
) -> None:
    event_id = uuid4()

    response = await client.post(
        "/events", json=_event(event_id), headers={AGENT_TOKEN_HEADER: TOKEN}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    async with db.session() as session:
        marker = await session.get(ProcessedEvent, (event_id, SERVICE_NAME))
    assert marker is not None, "the handler must record what it handled"


async def test_redelivery_is_a_no_op(client: httpx.AsyncClient, db: Database) -> None:
    """The same event twice is handled once — and answered 200, so the worker stops."""
    body = _event()

    headers = {AGENT_TOKEN_HEADER: TOKEN}
    first = await client.post("/events", json=body, headers=headers)
    second = await client.post("/events", json=body, headers=headers)

    assert first.json() == {"status": "processed"}
    assert second.json() == {"status": "already_processed"}
    assert await _processed_count(db) == 1


async def test_an_event_for_another_agent_is_marked_seen_and_dropped(
    client: httpx.AsyncClient, db: Database
) -> None:
    """An error would have the worker retry to exhaustion and dead-letter it.

    ``alert.normalized`` is the watcher's event, not the planner's. Marked
    processed and dropped, so it is dispatched exactly once and then never again.
    """
    response = await client.post(
        "/events",
        json=_event(event_type="alert.normalized"),
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

    The case a startup-state-only probe gets wrong: it reports ready, Kubernetes
    routes traffic to a pod that cannot touch its database, and the failure
    surfaces as errors to callers instead of a pod pulled from service.
    """
    app = app_factory(_secrets(tmp_path, dsn=UNREACHABLE_DSN))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://planner"
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
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://planner"
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

    The distinction matters to the worker: 503 is retryable (it backs off and
    tries again once the pod is ready), while 401 is permanent and would
    dead-letter the event outright over what is only a slow start.
    """
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://planner"
        ) as http:
            response = await http.post(
                "/events", json=_event(), headers={AGENT_TOKEN_HEADER: TOKEN}
            )

    assert response.status_code == 503
