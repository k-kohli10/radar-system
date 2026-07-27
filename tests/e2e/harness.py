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

``feedback-service`` IS wired, so ``recommendation.created`` is delivered rather than
dead-lettered and the outbox drains to empty. Slack is the one thing it cannot have for
real, and both directions are substituted at seams the service already exposes for
exactly this:

- **outbound** — ``notifier_override`` takes a :class:`RecordingNotifier`, a structural
  ``NotificationBackend`` that keeps the card instead of posting it. So a test can read
  the card the engineer would have seen.
- **inbound** — ``interaction_source_factory`` takes a socketless source that captures
  the REAL interaction handler the service built. :meth:`Pipeline.click` then drives a
  button press straight into it. No WebSocket, no workspace, and the code under test is
  the production handler, not a re-implementation.

The vendor-neutral ``NotificationInteraction`` contract is what makes this honest: it is
exactly what the Slack plugin hands the service, so nothing Slack-shaped is faked beyond
the transport.
"""

from __future__ import annotations

import asyncio
import json
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
from radar_common import REASONER_DISPATCH_TIMEOUT_SECONDS
from radar_contracts import LLMMode, LLMResponse, NotificationInteraction, Usage
from radar_database import Database, claim_outbox_batch
from radar_feedback_service.interactions import InteractionHandler, MentionHandler
from radar_feedback_service.main import create_app as create_feedback_app
from radar_ingestion.main import create_app as create_ingestion_app
from radar_knowledge_service.main import create_app as create_knowledge_app
from radar_llm_gateway.main import create_app as create_gateway_app
from radar_outbox_worker.dispatcher import (
    EventDispatcher,
    TargetResolver,
    TimeoutPolicy,
)
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
#: Prometheus gets its OWN token: ingestion checks the token per source, so a token
#: valid for /alerts/mock is 401 on /alerts/prometheus (ADR 0011).
PROMETHEUS_WEBHOOK_TOKEN = "e2e-prom-token-" + "p" * 49

WEBHOOK_HEADER = "X-Radar-Webhook-Token"

#: The agents the worker dispatches to, and the host each is reachable at in-process.
AGENT_SERVICES = ("watcher-agent", "planner-agent", "reasoner-agent")

FEEDBACK_SERVICE = "feedback-service"

#: EVERY dispatch target, agents plus the terminal consumer. The worker needs a route
#: and a token for each; keeping this separate from ``AGENT_SERVICES`` preserves that
#: tuple's meaning ("the pipeline's agents") now that a non-agent is also a target.
DISPATCH_SERVICES = (*AGENT_SERVICES, FEEDBACK_SERVICE)

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


# --- the fake Slack side of feedback-service -----------------------------------


class RecordingNotifier:
    """A structural ``NotificationBackend`` that keeps the card instead of posting it.

    Conformance is structural (the contract is a ``runtime_checkable`` Protocol), so no
    Slack SDK is imported and nothing here can drift from the real backend's signature
    without mypy saying so at the injection site.

    ``send`` returns a distinct message ref per call, as Slack's ``ts`` is: the ref is
    what a click echoes back, so reusing one would let a test pass while pointing every
    interaction at the same card.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        ref = f"1720000000.{len(self.sent) + 1:04d}"
        self.sent.append(
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "thread_ref": thread_ref,
                "ts": ref,
            }
        )
        return ref

    async def update(
        self,
        channel: str,
        message_ref: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.updates.append(
            {
                "channel": channel,
                "message_ref": message_ref,
                "text": text,
                "blocks": blocks,
            }
        )


@dataclass
class SlackControl:
    """The Slack surface, as a test sees it: what was delivered, and the way back in.

    ``interaction`` and ``mention`` are the service's OWN handlers, captured when its
    lifespan started the source. They are ``None`` until then — a test that finds them
    unset is looking at a service whose lifespan never ran, which is worth failing on
    rather than skipping past.
    """

    notifier: RecordingNotifier
    interaction: InteractionHandler | None = None
    mention: MentionHandler | None = None

    def card_for(self, recommendation_id: UUID) -> dict[str, Any]:
        """The delivered card whose buttons act on ``recommendation_id``.

        Located by the id the card round-trips in its button values — the same thing
        Slack echoes back on a click — so this finds the card the way the real callback
        path identifies it. Raises rather than returning ``None``: "no card was
        delivered" must fail the test that asked for one, not quietly become a
        comparison against nothing.
        """
        wanted = str(recommendation_id)
        for card in self.notifier.sent:
            if wanted in json.dumps(card["blocks"] or []):
                return card
        raise AssertionError(
            f"no delivered card carries recommendation {wanted}; "
            f"{len(self.notifier.sent)} card(s) were sent"
        )


class _CapturingInteractionSource:
    """A socketless ``InteractionSource`` that hands the real handlers to the test.

    The production source opens a WebSocket to Slack on ``start``; this records the
    handlers the lifespan built and returns. Nothing about the handlers is faked — they
    close over the real database, the real notifier and the real metrics.
    """

    def __init__(
        self,
        control: SlackControl,
        handler: InteractionHandler,
        mention_handler: MentionHandler,
    ) -> None:
        self._control = control
        self._handler = handler
        self._mention_handler = mention_handler

    async def start(self) -> None:
        self._control.interaction = self._handler
        self._control.mention = self._mention_handler

    async def close(self) -> None:
        return None


# --- the routing transport (worker → in-process agents) -----------------------


class _RoutingTransport(httpx.AsyncBaseTransport):
    """Routes an outbound request to the in-process app for its URL host.

    An unknown host raises ``ConnectError`` — exactly what an unreachable service is,
    and what makes a dispatch to a service this harness does not run fail the way a
    real missing service would rather than being silently delivered.
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
    #: The mock gateway's control, or ``None`` on the live pipeline (real gateway).
    gateway: GatewayControl | None
    _ingestion: httpx.AsyncClient
    _processor: DispatchProcessor
    _worker_db: Database
    apps: dict[str, FastAPI]
    #: feedback-service's Slack surface: delivered cards, and the click entry point.
    slack: SlackControl
    #: Correlation ids of every outbox event the drain claimed.
    #:
    #: The outbox is a QUEUE — ``mark_dispatched`` deletes a delivered event — so
    #: after a full drain no row is left to read the id off. Recorded as they pass,
    #: which is what lets the traceability test still assert the invariant, and over
    #: all four hand-offs rather than the one event left behind by failing.
    dispatched_correlation_ids: list[UUID] = field(default_factory=list)

    @property
    def mock(self) -> GatewayControl:
        """The mock gateway's control. Only the deterministic pipeline has one."""
        assert self.gateway is not None, (
            "this Pipeline has no mock gateway (it is live)"
        )
        return self.gateway

    async def post_alert(self, body: dict[str, Any] | None = None) -> httpx.Response:
        """POST one alert to ingestion — the pipeline's front door."""
        return await self._ingestion.post(
            "/alerts/mock",
            json=body if body is not None else MOCK_ALERT,
            headers={WEBHOOK_HEADER: WEBHOOK_TOKEN},
        )

    async def post_prometheus_alert(self, body: dict[str, Any]) -> httpx.Response:
        """POST one alertmanager-shaped alert to ``/alerts/prometheus``.

        The front door Prometheus itself would use, exercising the real Prometheus
        normalizer rather than the mock one — see the platform-sim alert-path test.
        """
        return await self._ingestion.post(
            "/alerts/prometheus",
            json=body,
            headers={WEBHOOK_HEADER: PROMETHEUS_WEBHOOK_TOKEN},
        )

    async def drain(self, *, max_iterations: int = 50) -> None:
        """Turn the real claim → dispatch → mark crank until the outbox settles.

        Each hop's handler commits its next outbox event before responding, so the next
        claim sees it: one loop walks the whole pipeline. Settled = a claim returns
        nothing due (everything dispatched, or scheduled for a future retry).
        """
        for _ in range(max_iterations):
            async with self._worker_db.session() as session:
                events = await claim_outbox_batch(session, limit=20)
                await session.commit()
            if not events:
                return
            self.dispatched_correlation_ids.extend(e.correlation_id for e in events)
            for event in events:
                await self._processor(event)
        raise AssertionError(
            f"the pipeline did not settle within {max_iterations} drain iterations"
        )

    async def click(
        self,
        action_id: str,
        *,
        recommendation_id: UUID,
        user_id: str = "U_E2E_ENGINEER",
    ) -> None:
        """Press one button on the delivered RCA card, through the REAL handler.

        Builds the vendor-neutral ``NotificationInteraction`` the Slack plugin would
        have built from a ``block_actions`` payload and hands it to the handler
        feedback-service itself constructed. The channel and message ref come from the
        card that was actually delivered, so a click can only ever reference a card this
        pipeline really posted.
        """
        handler = self.slack.interaction
        assert handler is not None, (
            "feedback-service's interaction handler was never captured — its lifespan "
            "did not start the source"
        )
        card = self.slack.card_for(recommendation_id)
        await handler(
            NotificationInteraction(
                action_id=action_id,
                value=str(recommendation_id),
                user_id=user_id,
                channel_id=card["channel"],
                message_ts=card["ts"],
            )
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
    (directory / "webhook_token_prometheus").write_text(PROMETHEUS_WEBHOOK_TOKEN)


async def _assemble_services(
    stack: AsyncExitStack,
    db: Database,
    database_url: str,
    gateway: GatewayControl | None,
) -> Pipeline:
    """Build the pipeline's services + the real worker + the ingestion client.

    Assumes ``RADAR_SECRETS_DIR`` and ``RADAR_GATEWAY_URL`` are already set and the
    gateway (mock or real) is already serving. Shared by the mock and live builders so
    the *pipeline* is identical either way — only the gateway behind it differs.

    feedback-service is built with both Slack seams substituted, so it needs no bot
    token and opens no socket; everything else about it is the real service.
    """
    slack = SlackControl(notifier=RecordingNotifier())

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
        FEEDBACK_SERVICE: create_feedback_app(
            metrics_registry=CollectorRegistry(),
            with_tracing=False,
            notifier_override=slack.notifier,
            interaction_source_factory=lambda handler, mention: (
                _CapturingInteractionSource(slack, handler, mention)
            ),
        ),
    }
    for app in apps.values():
        await stack.enter_async_context(app.router.lifespan_context(app))

    routes = {
        service: httpx.ASGITransport(app=apps[service]) for service in DISPATCH_SERVICES
    }
    worker_client = await stack.enter_async_context(
        httpx.AsyncClient(transport=_RoutingTransport(routes))
    )
    dispatcher = EventDispatcher(
        worker_client,
        TargetResolver(overrides={s: f"http://{s}/events" for s in DISPATCH_SERVICES}),
        DispatchTokenMap({s: AGENT_TOKEN for s in DISPATCH_SERVICES}),
        # THE catch that keeps the live latency honest: the reasoner hop calls an LLM
        # and can take tens of seconds, so the worker must wait the real 90s dispatch
        # budget — the production value, ordered above the reasoner's own 60s. A shorter
        # default would fire mid-call and record a dispatch timeout that never happened.
        # Harmless for the fast deterministic tests, which answer in milliseconds.
        timeouts=TimeoutPolicy(
            overrides={"reasoner-agent": REASONER_DISPATCH_TIMEOUT_SECONDS}
        ),
    )
    worker_db = Database(database_url)
    stack.push_async_callback(worker_db.dispose)

    ingestion_client = await stack.enter_async_context(
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps["ingestion"]),
            base_url="http://ingestion",
        )
    )

    return Pipeline(
        db=db,
        gateway=gateway,
        _ingestion=ingestion_client,
        _processor=DispatchProcessor(worker_db, dispatcher),
        _worker_db=worker_db,
        apps=apps,
        slack=slack,
    )


@asynccontextmanager
async def build_pipeline(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Pipeline]:
    """The deterministic pipeline: a MOCK gateway and the compressed budget."""
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
        gateway_app, control = _build_mock_gateway()
        gateway_url = await stack.enter_async_context(_serve(gateway_app))
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)

        yield await _assemble_services(stack, db, database_url, control)


# --- the live pipeline (real llm-gateway → real OpenAI) -----------------------


GATEWAY_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "llm-gateway"
    / "config"
    / "gateway.yaml"
)
"""The repo's real mode-routing config — extended → OpenAI gpt-4o."""


EMBED_TOKEN = "e" * 64
"""The knowledge service's embed-mode gateway token (one token = one mode)."""

REASON_TOKEN = "n" * 64
"""The knowledge service's reason-mode gateway token (CRAG grading)."""


def _gateway_tokens_yaml() -> str:
    """The ``gateway_tokens`` secret: every grant the pipeline's callers hold.

    The knowledge entries are present even when the pipeline runs without a
    knowledge service — unused grants are harmless, and one yaml means the two
    live builders cannot drift.
    """
    return (
        "tokens:\n"
        f"  {GATEWAY_TOKEN}:\n"
        "    service: reasoner-agent\n"
        "    allowed_mode: extended\n"
        f"  {EMBED_TOKEN}:\n"
        "    service: knowledge-service\n"
        "    allowed_mode: embed\n"
        f"  {REASON_TOKEN}:\n"
        "    service: knowledge-service\n"
        "    allowed_mode: reason\n"
    )


@asynccontextmanager
async def build_live_pipeline(
    db: Database,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    openai_api_key: str,
    with_knowledge: bool = False,
) -> AsyncIterator[Pipeline]:
    """The live pipeline: the REAL llm-gateway calling REAL OpenAI, real 60s budget.

    ``with_knowledge`` additionally serves the real knowledge service against the
    real Elasticsearch index, and points the reasoner at it — the full Phase 8
    path: retrieval, RRF, CRAG, and a grounded (or honestly empty) bundle.

    No mock and no compressed budget — the whole point is to measure the real extended
    call against the real budget. The gateway runs on a real socket (like the mock), but
    it is the actual ``radar_llm_gateway`` app, keyed with a real OpenAI key.
    """
    secrets = tmp_path / "secrets"
    _write_secrets(secrets, dsn=database_url)
    # The gateway's own secrets, alongside the agents' in the shared dir.
    (secrets / "openai_api_key").write_text(openai_api_key)
    (secrets / "gateway_tokens").write_text(_gateway_tokens_yaml())
    if with_knowledge:
        # The knowledge service's two outbound gateway tokens, and the
        # reasoner's copy of the knowledge service's inbound token — which, in
        # this harness's one-shared-token simplification, is AGENT_TOKEN.
        (secrets / "gateway_token_embed").write_text(EMBED_TOKEN)
        (secrets / "gateway_token_reason").write_text(REASON_TOKEN)
        (secrets / "knowledge_token").write_text(AGENT_TOKEN)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(secrets))
    monkeypatch.setenv("RADAR_GATEWAY_CONFIG_PATH", str(GATEWAY_CONFIG_PATH))

    async with AsyncExitStack() as stack:
        gateway_app = create_gateway_app(
            metrics_registry=CollectorRegistry(), with_tracing=False
        )
        gateway_url = await stack.enter_async_context(_serve(gateway_app))
        monkeypatch.setenv("RADAR_GATEWAY_URL", gateway_url)

        if with_knowledge:
            # The knowledge service, on a real socket like the gateway and for
            # the same reason: the reasoner builds its own httpx client from
            # RADAR_KNOWLEDGE_URL and there is no injection point. Built after
            # the gateway (its lifespan reads RADAR_GATEWAY_URL) and before the
            # agents (the reasoner reads RADAR_KNOWLEDGE_URL at build time).
            knowledge_app = create_knowledge_app(
                metrics_registry=CollectorRegistry(), with_tracing=False
            )
            knowledge_url = await stack.enter_async_context(_serve(knowledge_app))
            monkeypatch.setenv("RADAR_KNOWLEDGE_URL", knowledge_url)

        # No budget compression: the reasoner runs its real 60s budget.
        yield await _assemble_services(stack, db, database_url, None)


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
