"""The in-process pipeline harness: ingestion → watcher → planner → reasoner, for real.

The building blocks the e2e fixtures assemble. This module is the *machinery*; the thin
``conftest.py`` wraps :func:`build_pipeline` in a fixture. Splitting them keeps the
fixtures file to fixtures and lets the tests import the helper types (``Pipeline``,
``GatewayControl``) without importing a conftest.

Everything here is real except the LLM provider:

- **Real Postgres.** The same database every service reads and writes.
- **Real services.** ingestion, watcher, planner and reasoner are their actual FastAPI
  apps, built by their own ``create_app`` and driven through their real lifespans.
- **The real outbox-worker.** The actual ``EventDispatcher`` + ``DispatchProcessor``,
  doing the real claim → dispatch → mark cycle; ``Pipeline.drain`` turns the crank.
- **A mock llm-gateway, on a real socket.** The reasoner builds its own httpx client
  from ``gateway_url`` with ``timeout=None`` and there is no injection point, so a real
  uvicorn server is the faithful choice: it exercises the actual client the reliability
  argument depends on. The mock speaks the real ``LLMResponse`` contract, so a gateway
  contract change breaks the e2e rather than passing against a fiction.

TWO DELIBERATE SIMPLIFICATIONS, BOTH STATED
-------------------------------------------
1. **One shared agent token.** The four services run in one process and read one
   ``RADAR_SECRETS_DIR``, so they share one inbound ``agent_token`` and the worker
   dispatches to every target with that value. Per-service token isolation is proven in
   each agent's own ``test_*_events_api.py``; this harness proves the PIPELINE.
2. **The worker's hops are in-process ASGI; only the gateway is a real socket.** The
   worker's client is ours to build, so its hops route through an ASGI transport. The
   reasoner→gateway hop cannot be injected, so that one is a real socket. The split
   follows from where the code lets us inject, not from a corner we chose to cut.

THE COMPRESSED LLM BUDGET
-------------------------
The reasoner's real budget is 60s (``REASONER_LLM_BUDGET_SECONDS``), and there is no
setting to shrink it — deliberately, so nobody can raise it past the worker's 90s and
slip by the import-time ordering assertion. To exercise the *timeout* fallback without a
minute-long wait, the harness monkeypatches ``GatewayClient`` construction to inject a
:data:`COMPRESSED_BUDGET_SECONDS` budget (the technique R4's timeout unit test uses,
applied at the app boundary). Instant mock responses finish in milliseconds and are
unaffected; only the deliberately-slow responder outruns it. The real 60s/90s ordering
stays asserted where it belongs, at import in ``radar_common``.

``feedback-service`` does not exist until Phase 9, so ``recommendation.created`` has no
route: the worker's dispatch to it fails and the event dead-letters. That is correct;
the pipeline's PROOF is the recommendation ROW, read straight from Postgres.
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

COMPRESSED_BUDGET_SECONDS = 1.0
"""The reasoner's LLM budget under test — see the module docstring.

Small enough that the ``timeout`` trigger fires in ~1s instead of 60, large enough that
an instant mock response (milliseconds) never trips it by accident.
"""

TIMEOUT_DELAY_SECONDS = 3.0
"""How long the slow responder sleeps: comfortably past the compressed budget, so the
reasoner's ``asyncio.timeout`` is provably the bound that fires."""


# --- the mock gateway ---------------------------------------------------------


Responder = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


@dataclass
class GatewayControl:
    """Decides what the mock llm-gateway returns, per test.

    ``respond`` maps the reasoner's request body to ``(status_code, json_body)``;
    ``delay_seconds`` sleeps first, for the timeout trigger. The default is a valid
    completion. ``received`` records every request, so a test can assert the reasoner
    actually called out.
    """

    respond: Responder
    delay_seconds: float = 0.0
    received: list[dict[str, Any]] = field(default_factory=list)


def _llm_response(content: str) -> dict[str, Any]:
    """A 200 body carrying the real ``LLMResponse`` contract with ``content``."""
    return LLMResponse(
        id="resp_e2e",
        mode=LLMMode.EXTENDED,
        provider="mock-openai",
        model="mock-gpt-4o",
        content=content,
        usage=Usage(prompt_tokens=128, completion_tokens=42),
        latency_ms=1234,
    ).model_dump(mode="json")


def success(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """A usable RCA — the one path that does NOT fall back."""
    return 200, _llm_response(RCA_JSON)


def unavailable(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Every provider failed → the gateway's 503 → ``gateway_unavailable``."""
    return 503, {"detail": "every provider failed"}


def rejected(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The gateway rejects US (bad token / mode) → 401 → ``rejected``.

    OUR misconfiguration, the reason that must stay distinct from an outage: it means
    fix the config now, not wait for a provider.
    """
    return 401, {"detail": "unauthorized"}


def prose(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The model apologizes in prose instead of JSON → ``not_json``."""
    return 200, _llm_response(
        "I'm sorry, I can't determine the root cause from the information provided."
    )


def bad_shape(_body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Valid JSON, but not the RCA schema → ``schema_invalid``.

    No ``root_cause``, an unknown confidence, and zero actions — each on its own is a
    refusal, and the point is that it IS json (so not ``not_json``) yet unusable.
    """
    return 200, _llm_response('{"confidence": "very high", "recommended_actions": []}')


def _build_mock_gateway() -> tuple[FastAPI, GatewayControl]:
    control = GatewayControl(respond=success)
    app = FastAPI()

    @app.post("/v1/complete")
    async def complete(request: Request) -> JSONResponse:
        body = await request.json()
        control.received.append(body)
        if control.delay_seconds:
            await asyncio.sleep(control.delay_seconds)
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

    An unknown host raises ``ConnectError`` — exactly what an unreachable service is.
    ``feedback-service`` (Phase 9) has no route, so the worker's dispatch to it fails
    the way a real missing service would, and the event dead-letters rather than being
    silently delivered.
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


# --- the compressed-budget injection ------------------------------------------


def _compressed_gateway_client(client: httpx.AsyncClient, token: Any) -> Any:
    """A ``GatewayClient`` with the compressed budget — the monkeypatch target.

    Matches the two-positional-arg call the reasoner's ``main`` makes, and injects the
    budget it does not pass. See the module docstring for why the budget is compressed.
    """
    from radar_reasoner_agent.llm import GatewayClient

    return GatewayClient(client, token, budget_seconds=COMPRESSED_BUDGET_SECONDS)


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
            f"the pipeline did not settle within {max_iterations} drain iterations"
        )

    async def scrape(self, service: str) -> str:
        """GET ``/metrics`` from one in-process app."""
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


@asynccontextmanager
async def build_pipeline(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    """Assemble the whole pipeline for one test. The fixture just wraps this."""
    import radar_reasoner_agent.main as reasoner_main

    secrets = tmp_path / "secrets"
    _write_secrets(secrets, dsn=database_url)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets))

    # The reasoner's budget is compressed for the timeout trigger — see the docstring.
    # Patched BEFORE the reasoner app is built, so its lifespan constructs the
    # compressed client.
    monkeypatch.setattr(reasoner_main, "GatewayClient", _compressed_gateway_client)

    async with AsyncExitStack() as stack:
        # The mock gateway first, on a real port, so its URL is known before the
        # reasoner reads RADAR_GATEWAY_URL at build time.
        gateway_app, gateway = _build_mock_gateway()
        gateway_url = await stack.enter_async_context(_serve(gateway_app))
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)

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
