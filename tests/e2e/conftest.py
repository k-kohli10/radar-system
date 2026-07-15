"""The in-process pipeline harness: ingestion → watcher → planner → reasoner, for real.

The first test that runs the WHOLE pipeline as one system, not one service at a time.
Everything below is real except the LLM provider:

- **Real Postgres.** The same database every service reads and writes; the outbox is a
  real table, the correlation chain is real rows.
- **Real services.** ingestion, watcher, planner and reasoner are their actual FastAPI
  apps, built by their own ``create_app`` and driven through their real lifespans — the
  same startup that loads secrets and opens the database in production.
- **The real outbox-worker.** Not a test pump: the actual ``EventDispatcher`` +
  ``DispatchProcessor``, doing the real claim → dispatch → mark cycle. ``drain()`` just
  turns the crank until the outbox settles.
- **A mock llm-gateway, on a real socket.** The reasoner builds its own httpx client
  from ``gateway_url`` with ``timeout=None`` — there is no injection point, and that is
  the point: a real uvicorn server exercises the actual client the reliability argument
  depends on, not a stand-in. The mock speaks the real ``LLMResponse`` contract (see
  :class:`GatewayControl`), so a gateway contract change breaks this test rather than
  letting it pass against a shape the gateway no longer emits.

TWO DELIBERATE SIMPLIFICATIONS, BOTH STATED
-------------------------------------------
1. **One shared agent token.** The four services run in one process and read one
   ``RADAR_SECRETS_DIR``, so they share a single inbound ``agent_token``, and the worker
   dispatches to every target with that same value. The per-service token model
   (distinct token per agent, 401 on the wrong one) is real in production and is proven
   per-service in each agent's own ``test_*_events_api.py``. This harness proves the
   PIPELINE, not the token isolation, and collapsing the tokens keeps it from re-testing
   what those already pin.
2. **The worker's hops are in-process ASGI; only the gateway is a real socket.** The
   worker's client is ours to build, so its hops route through an ASGI transport with no
   ports. The reasoner→gateway hop cannot be injected, so that one is a real socket. The
   split follows from where the code lets us inject, not from a corner we chose to cut.

``feedback-service`` does not exist until Phase 9, so ``recommendation.created`` has no
route: the worker's dispatch to it fails and the event dead-letters. That is correct,
and the pipeline's PROOF is the recommendation ROW, which this harness reads straight
from Postgres — never the delivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry
from radar_contracts import LLMMode, LLMResponse, Usage
from radar_database import Database, claim_outbox_batch
from radar_ingestion.main import create_app as create_ingestion_app
from radar_outbox_worker.dispatcher import EventDispatcher, TargetResolver
from radar_outbox_worker.retry import DispatchProcessor
from radar_outbox_worker.security import DispatchTokenMap
from radar_planner_agent.main import create_app as create_planner_app
from radar_reasoner_agent.main import create_app as create_reasoner_app
from radar_testing.postgres import database_url, db  # noqa: F401  (fixtures)
from radar_watcher_agent.main import create_app as create_watcher_app
from sqlalchemy import select

# One shared token across the in-process services — see the module docstring.
AGENT_TOKEN = "e2e-agent-token-" + "e" * 48
GATEWAY_TOKEN = "e2e-gateway-token-" + "g" * 46
WEBHOOK_TOKEN = "e2e-webhook-token-" + "w" * 46

WEBHOOK_HEADER = "X-Radar-Webhook-Token"

#: The agents the worker dispatches to, and the host each is reachable at in-process.
AGENT_SERVICES = ("watcher-agent", "planner-agent", "reasoner-agent")

MOCK_ALERT = {
    "service_name": "order-service",
    "alert_name": "OrderProcessingFailureRate",
    "severity": "critical",
}

#: A valid RCA the parser accepts — the success path's model output.
RCA_JSON = (
    '{"root_cause": "A recent deploy to order-service broke order validation.", '
    '"confidence": "high", "recommended_actions": ['
    '{"order": 1, "action": "kubectl rollout undo deployment/order-service"}, '
    '{"order": 2, "action": "check order-service error logs in Kibana"}]}'
)


# --- the mock gateway ---------------------------------------------------------


@dataclass
class GatewayControl:
    """Decides what the mock llm-gateway returns, per test.

    ``respond`` maps the reasoner's request body to ``(status_code, json_body)``. The
    default is a valid completion; E2 swaps it for 503 / prose / bad-shape to drive the
    fallback triggers through the full pipeline. ``received`` records every request, so
    a test can assert the reasoner actually called out (and did not, say, fall back
    before trying).
    """

    respond: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
    received: list[dict[str, Any]] = field(default_factory=list)


def _success(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """A 200 carrying the real ``LLMResponse`` contract — not hand-rolled JSON."""
    response = LLMResponse(
        id="resp_e2e",
        mode=LLMMode.EXTENDED,
        provider="mock-openai",
        model="mock-gpt-4o",
        content=RCA_JSON,
        usage=Usage(prompt_tokens=128, completion_tokens=42),
        latency_ms=1234,
    )
    return 200, response.model_dump(mode="json")


def _build_mock_gateway() -> tuple[FastAPI, GatewayControl]:
    control = GatewayControl(respond=_success)
    app = FastAPI()

    @app.post("/v1/complete")
    async def complete(request: Request) -> JSONResponse:
        body = await request.json()
        control.received.append(body)
        status_code, payload = control.respond(body)
        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app, control


@asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[str]:
    """Run ``app`` on a real ephemeral localhost port; yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # startup is async; wait for the socket to bind
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


# --- the routing transport (worker → in-process agents) -----------------------


class _RoutingTransport(httpx.AsyncBaseTransport):
    """Routes an outbound request to the in-process app for its URL host.

    An unknown host raises ``ConnectError`` — which is exactly what an unreachable
    service is. ``feedback-service`` (Phase 9) has no route, so the worker's dispatch
    to it fails the way a real missing service would, and the event dead-letters rather
    than being silently delivered.
    """

    def __init__(self, routes: dict[str, httpx.ASGITransport]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._routes.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(
                f"no in-process route for {request.url.host!r}", request=request
            )
        return await transport.handle_async_request(request)


# --- the harness --------------------------------------------------------------


@dataclass
class Pipeline:
    """Everything a pipeline test needs: an entry point, a crank, and the database."""

    db: Database
    gateway: GatewayControl
    _ingestion: httpx.AsyncClient
    _processor: DispatchProcessor
    _worker_db: Database
    apps: dict[str, FastAPI]

    async def post_alert(self, body: dict[str, Any] | None = None) -> httpx.Response:
        """POST one alert to ingestion — the pipeline's front door."""
        return await self._ingestion.post(
            "/alerts/mock",
            json=body if body is not None else MOCK_ALERT,
            headers={WEBHOOK_HEADER: WEBHOOK_TOKEN},
        )

    async def drain(self, *, max_iterations: int = 50) -> None:
        """Turn the real claim → dispatch → mark crank until the outbox settles.

        Each hop's handler commits its next outbox event before responding, so the next
        claim sees it: one loop walks the whole pipeline. Settled = a claim returns
        nothing due (everything dispatched, or scheduled for a future retry like the
        Phase-9 ``recommendation.created``).
        """
        for _ in range(max_iterations):
            async with self._worker_db.session() as session:
                events = await claim_outbox_batch(session, limit=20)
                await session.commit()
            if not events:
                return
            for event in events:
                await self._processor(event)
        raise AssertionError(
            "the pipeline did not settle within "
            f"{max_iterations} drain iterations — a hop is looping"
        )

    async def scrape(self, service: str) -> str:
        """GET ``/metrics`` from one in-process app (used by E2's label assertions)."""
        transport = httpx.ASGITransport(app=self.apps[service])
        async with httpx.AsyncClient(
            transport=transport, base_url=f"http://{service}"
        ) as client:
            return (await client.get("/metrics")).text


def _write_secrets(directory: Path, *, dsn: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "postgres_dsn").write_text(dsn)
    (directory / "agent_token").write_text(AGENT_TOKEN)
    (directory / "gateway_token").write_text(GATEWAY_TOKEN)
    (directory / "webhook_token_mock").write_text(WEBHOOK_TOKEN)


@pytest_asyncio.fixture
async def pipeline(
    db: Database,  # noqa: F811  (fixture)
    database_url: str,  # noqa: F811  (fixture)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    secrets = tmp_path / "secrets"
    _write_secrets(secrets, dsn=database_url)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets))

    async with AsyncExitStack() as stack:
        # The mock gateway first, on a real port, so its URL is known before the
        # reasoner reads RADAR_GATEWAY_URL at build time.
        gateway_app, gateway = _build_mock_gateway()
        gateway_url = await stack.enter_async_context(_serve(gateway_app))
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)

        # The four services, each with its own metrics registry (no global collisions)
        # and its real lifespan (secrets load, database opens).
        apps: dict[str, FastAPI] = {
            "ingestion": create_ingestion_app(
                metrics_registry=CollectorRegistry(), with_tracing=False
            ),
            "watcher-agent": create_watcher_app(
                metrics_registry=CollectorRegistry(), with_tracing=False
            ),
            "planner-agent": create_planner_app(
                metrics_registry=CollectorRegistry(), with_tracing=False
            ),
            "reasoner-agent": create_reasoner_app(
                metrics_registry=CollectorRegistry(), with_tracing=False
            ),
        }
        for app in apps.values():
            await stack.enter_async_context(app.router.lifespan_context(app))

        # The worker's dispatch client routes each target_service to its in-process app.
        routes = {
            service: httpx.ASGITransport(app=apps[service])
            for service in AGENT_SERVICES
        }
        worker_client = await stack.enter_async_context(
            httpx.AsyncClient(transport=_RoutingTransport(routes))
        )
        dispatcher = EventDispatcher(
            worker_client,
            TargetResolver(overrides={s: f"http://{s}/events" for s in AGENT_SERVICES}),
            DispatchTokenMap({s: AGENT_TOKEN for s in AGENT_SERVICES}),
        )
        worker_db = Database(database_url)
        stack.push_async_callback(worker_db.dispose)

        ingestion_client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps["ingestion"]),
                base_url="http://ingestion",
            )
        )

        yield Pipeline(
            db=db,
            gateway=gateway,
            _ingestion=ingestion_client,
            _processor=DispatchProcessor(worker_db, dispatcher),
            _worker_db=worker_db,
            apps=apps,
        )


# --- read helpers -------------------------------------------------------------


async def correlation_ids(
    database: Database, table: type[Any], **where: Any
) -> list[UUID]:
    """Every ``correlation_id`` on ``table`` (optionally filtered)."""
    stmt = select(table.correlation_id)
    for column, value in where.items():
        stmt = stmt.where(getattr(table, column) == value)
    async with database.session() as session:
        return list(await session.scalars(stmt))
