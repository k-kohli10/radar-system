"""The reasoner's ``POST /events`` contract, and the readiness probe guarding it.

Driven through the real app and the real lifespan against real Postgres.

THIRD consumer of the shared auth guard (``radar_common.auth``), and that is why
these tests are not redundant with the watcher's and planner's: a guard that works
for two services and silently not a third is exactly what an extraction produces, and
only per-service tests catch it.

TWO READINESS RULES THAT PULL IN OPPOSITE DIRECTIONS, BOTH PINNED HERE

**The gateway is NOT gated.** The reasoner exists to keep working when the LLM does
not — a gateway outage is precisely when its template fallback matters. Gating
readiness on the gateway would take the reasoner out of rotation at the exact moment
its fallback is needed, and no incident would get any RCA at all. The cure would cause
the disease. So the reasoner must be READY with the gateway unreachable.

**The gateway TOKEN is gated.** Without it every LLM call is a 401, every incident
silently takes the fallback path, and the service looks perfectly healthy while never
once using the model it exists to use. That is a quality failure nobody notices, so it
is made a deploy-time error.

The two look contradictory and are not: one is an upstream we degrade around, the
other is a secret we cannot function correctly without.
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
from radar_contracts import ReasoningRequestedPayload
from radar_database import Database, ProcessedEvent
from radar_reasoner_agent.config import SERVICE_NAME
from radar_reasoner_agent.main import create_app
from sqlalchemy import func, select

TOKEN = "r" * 64
WRONG_TOKEN = "x" * 64
GATEWAY_TOKEN = "g" * 64
UNREACHABLE_DSN = "postgresql+asyncpg://radar:radar@127.0.0.1:1/radar"
"""Port 1: nothing listens there, so the ping fails fast rather than hanging."""

UNREACHABLE_GATEWAY = "http://127.0.0.1:1"
"""Also nothing. The reasoner must be READY anyway — that is the whole point."""


def _secrets(
    tmp_path: Path,
    *,
    dsn: str,
    token: str | None = TOKEN,
    gateway_token: str | None = GATEWAY_TOKEN,
) -> Path:
    (tmp_path / "postgres_dsn").write_text(dsn)
    if token is not None:
        (tmp_path / "agent_token").write_text(token)
    if gateway_token is not None:
        (tmp_path / "gateway_token").write_text(gateway_token)
    return tmp_path


@pytest.fixture
def app_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build a reasoner app against a given secrets directory."""

    def build(secrets_dir: Path, *, gateway_url: str = UNREACHABLE_GATEWAY) -> Any:
        monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets_dir))
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)
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
            transport=transport, base_url="http://reasoner"
        ) as http:
            yield http


def _event(event_id: UUID | None = None, **overrides: Any) -> dict[str, Any]:
    """A well-formed reasoning_requested envelope, built from the real contract."""
    payload = ReasoningRequestedPayload(incident_id=uuid4(), plan_id=uuid4())
    body: dict[str, Any] = {
        "event_id": str(event_id or uuid4()),
        "event_type": "incident.reasoning_requested",
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
    assert await _processed_count(db) == 0


async def test_the_inbound_token_is_not_the_gateway_token(
    client: httpx.AsyncClient,
) -> None:
    """The two tokens travel in the SAME header name and are different values.

    Presenting the reasoner's OUTBOUND gateway token to its own inbound /events must
    be a 401. They are separate grants — one is this service's identity on the event
    bus, the other its authority to spend an expensive model — and a service that
    accepted either would have quietly merged two trust domains.
    """
    response = await client.post(
        "/events", json=_event(), headers={AGENT_TOKEN_HEADER: GATEWAY_TOKEN}
    )

    assert response.status_code == 401


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
    assert marker is not None


async def test_redelivery_is_a_no_op(client: httpx.AsyncClient, db: Database) -> None:
    """The gate matters more here than anywhere: a duplicate costs MONEY.

    The watcher and planner write rows; a duplicate wastes a transaction. The reasoner
    calls an LLM — a duplicate is a second bill and a second, contradictory RCA.
    """
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
    """incident.plan_requested is the planner's event, not the reasoner's."""
    response = await client.post(
        "/events",
        json=_event(event_type="incident.plan_requested"),
        headers={AGENT_TOKEN_HEADER: TOKEN},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await _processed_count(db) == 1


# --- readiness: the two rules that pull opposite ways --------------------------


async def test_readyz_is_ready_when_the_gateway_is_unreachable(
    client: httpx.AsyncClient,
) -> None:
    """THE readiness test for this service. The gateway is down; be ready anyway.

    The client fixture points the reasoner at a dead gateway (port 1) on purpose. A
    reasoner that refused to be ready here would be taken out of rotation exactly when
    its template fallback is the only thing keeping incidents from getting no analysis
    at all. The gateway is an upstream it degrades around, not a dependency it dies
    with.
    """
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_readyz_is_503_when_the_gateway_token_is_missing(
    tmp_path: Path, app_factory: Any, database_url: str
) -> None:
    """The mirror image: the TOKEN is gated even though the gateway is not.

    Without it, every LLM call is a 401 and every incident silently takes the fallback
    path. The service would look perfectly healthy while never once using the model it
    exists to use — a quality failure nobody notices. Make it a deploy-time error.
    """
    app = app_factory(_secrets(tmp_path, dsn=database_url, gateway_token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            response = await http.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # Probe-safe: it names the FILE, never a secret value.
    assert "gateway_token" in body["reason"]


async def test_readyz_is_503_when_the_database_is_unreachable(
    tmp_path: Path, app_factory: Any
) -> None:
    """Secrets all present, database dead — the probe must still say no."""
    app = app_factory(_secrets(tmp_path, dsn=UNREACHABLE_DSN))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            response = await http.get("/readyz")
            liveness = await http.get("/healthz")

    assert response.status_code == 503
    assert response.json()["reason"] == "database unreachable"
    # Liveness is a different question: the process is fine, so a DB blip must not
    # get the pod killed and restarted — only taken out of rotation.
    assert liveness.status_code == 200


async def test_readyz_is_503_when_the_agent_token_is_missing(
    tmp_path: Path, app_factory: Any, database_url: str
) -> None:
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            response = await http.get("/readyz")

    assert response.status_code == 503
    assert "agent_token" in response.json()["reason"]


async def test_events_is_503_before_the_secrets_load(
    tmp_path: Path, app_factory: Any, database_url: str
) -> None:
    """503 (retryable) — not 401, which the worker treats as permanent.

    A 401 during a slow start would have the worker dead-letter the event outright,
    losing an incident's RCA over a pod that was merely still booting.
    """
    app = app_factory(_secrets(tmp_path, dsn=database_url, token=None))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            response = await http.post(
                "/events", json=_event(), headers={AGENT_TOKEN_HEADER: TOKEN}
            )

    assert response.status_code == 503
