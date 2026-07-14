"""reasoner-agent service assembly.

The final stage of the pipeline, and the only one that calls out to a third party.
Everything it needs is loaded inside the lifespan — nothing at import time: the
Postgres DSN, the inbound ``agent_token``, and the outbound ``gateway_token``, all
from Vault. A missing secret leaves readiness false so ``/readyz`` answers 503
instead of crashing an import no probe will ever see.

``/readyz`` DOES NOT PING THE GATEWAY, AND THAT IS THE POINT
------------------------------------------------------------
The instinct is to check the upstream you depend on. It is wrong here, and backwards.

This service exists to keep working when the LLM does not. A gateway outage is
precisely when the template fallback matters — it is the only thing standing between
a dead provider and an incident with no analysis at all. If readiness depended on the
gateway, an outage would take the reasoner OUT of rotation at the exact moment its
fallback was needed, and no incident would get any RCA whatsoever. The cure would
cause the disease.

So readiness checks what the reasoner needs *to function*: its secrets, and its
database (pinged live on every call, because a probe that trusts its boot-time state
reports healthy with a dead database underneath it). The gateway is an upstream it
degrades around, not a dependency it dies with.

The ``gateway_token`` IS gated, though, and for the opposite reason: without it every
call would be rejected 401 and every incident would silently take the fallback path.
The reasoner would look perfectly healthy while never once using the LLM it exists to
use. That is a quality failure nobody notices, so it is made a deploy-time error.

``/healthz`` is process liveness only — deliberately NOT gated on the database, or a
database blip would get the pod killed and restarted rather than merely removed from
service.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status
from prometheus_client import REGISTRY, CollectorRegistry
from pydantic import SecretStr
from radar_common import (
    AgentTokenAuth,
    ConfigurationError,
    EventsAuth,
    bootstrap,
    install_guarded_events_handler,
    read_secret,
)
from radar_common.bootstrap import AGENT_TOKEN_SECRET
from radar_database import Database
from radar_telemetry import (
    create_request_metrics,
    instrument_fastapi,
    render_latest,
    setup_tracing,
)

from radar_reasoner_agent.config import (
    SERVICE_NAME,
    ReasonerSettings,
    load_gateway_token,
    load_postgres_dsn,
)
from radar_reasoner_agent.llm import GatewayClient
from radar_reasoner_agent.routes import create_events_router


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``.

    Starts not-ready ("starting"); :meth:`mark_ready` flips it once the Vault secrets
    have loaded. The live database check is separate (``/readyz`` pings on each call),
    so this tracks only the startup half of the contract. ``reason`` strings are safe
    to return to a probe — the config layer names files, never secret values.
    """

    def __init__(self) -> None:
        self._reason: str | None = "starting"

    def mark_ready(self) -> None:
        self._reason = None

    def mark_not_ready(self, reason: str) -> None:
        self._reason = reason or "not ready"

    @property
    def reason(self) -> str | None:
        return self._reason


def create_app(
    *,
    metrics_registry: CollectorRegistry = REGISTRY,
    with_tracing: bool = True,
) -> FastAPI:
    """Build the reasoner app. ``metrics_registry`` is injectable for tests."""
    # with_agent_auth=False: bootstrap would read the token at app-build time and
    # raise on a missing secret, which is exactly the crash /readyz exists to turn
    # into a 503. The token is loaded in the lifespan and late-bound below.
    runtime = bootstrap(ReasonerSettings, with_agent_auth=False)
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    request_metrics = create_request_metrics(metrics_registry)
    database: Database | None = None
    agent_auth: AgentTokenAuth | None = None
    gateway_token: SecretStr | None = None
    gateway_client: httpx.AsyncClient | None = None
    gateway: GatewayClient | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal database, agent_auth, gateway_token, gateway_client, gateway
        try:
            dsn = load_postgres_dsn()
            agent_token = read_secret(AGENT_TOKEN_SECRET)
            assert agent_token is not None  # required=True: raised if absent
            agent_auth = AgentTokenAuth([agent_token])
            # OUTBOUND, and a different value from the inbound one above. Gated on
            # readiness: without it every LLM call is a 401 and every incident
            # silently takes the fallback path, which looks perfectly healthy.
            gateway_token = load_gateway_token()
            # One client for the process, owned by the lifespan: the connection
            # pool is shared across requests rather than rebuilt per LLM call.
            #
            # timeout=None is DELIBERATE and load-bearing. httpx defaults to FIVE
            # SECONDS — an extended-mode LLM call routinely takes longer, so a client
            # left on the default would kill every call at 5s, every incident would
            # fall back to a template RCA, and the service would look perfectly
            # healthy while never once using the model it exists to use. There would
            # be no error to find. asyncio.timeout owns the clock (see llm.py), and
            # GatewayClient refuses to be built with a client that could undercut it.
            gateway_client = httpx.AsyncClient(
                base_url=settings.gateway_url, timeout=None
            )
            gateway = GatewayClient(gateway_client, gateway_token)
            database = Database(dsn)
            readiness.mark_ready()
            log.info("reasoner.ready", gateway_url=settings.gateway_url)
        except ConfigurationError as exc:
            # Config-layer messages are written to be secret-free.
            readiness.mark_not_ready(str(exc))
            log.error("reasoner.startup_failed", reason=str(exc))
        except Exception as exc:
            # Unexpected failure: expose the class only, never the message.
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("reasoner.startup_failed", error_type=type(exc).__name__)
        try:
            yield
        finally:
            # FIRST on shutdown, before draining: a probe mid-shutdown must see 503,
            # not 200, or traffic keeps arriving at a dying pod.
            readiness.mark_not_ready("shutting down")
            if gateway_client is not None:
                await gateway_client.aclose()
            if database is not None:
                await database.dispose()
            log.info("reasoner.shutdown")

    def get_database() -> Database | None:
        # Read per request: None until startup sets it (or if startup failed), so the
        # handler answers 503 rather than touching a missing database.
        return database

    def get_gateway() -> GatewayClient | None:
        # Late-bound like the rest. None until the token loads and the client is
        # built; the handler answers 503 rather than reasoning without an LLM it
        # could not even attempt to reach.
        return gateway

    def get_agent_auth() -> AgentTokenAuth | None:
        # Late-bound: None until the Vault secret loads, so the auth dependency
        # answers 503 while not ready and 401 once it is.
        return agent_auth

    events_auth = EventsAuth(get_agent_auth, service_name=SERVICE_NAME)

    app = FastAPI(title="radar-reasoner-agent", lifespan=lifespan)
    # Make 401 beat 422 on /events: a malformed body must not mask a bad token, and
    # an unauthenticated caller must not learn the shape of the contract.
    install_guarded_events_handler(app, events_auth)
    app.include_router(
        create_events_router(
            get_database=get_database,
            get_gateway=get_gateway,
            events_auth=events_auth,
        )
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only. NOT gated on the database: a DB blip should take the pod out
        # of rotation (readyz), not have it killed and restarted (healthz).
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        # Secrets loaded; now the live half. Asked every time, because a probe that
        # trusts its boot-time state reports healthy with a dead database under it.
        #
        # The GATEWAY is deliberately not checked — see the module docstring. A
        # gateway outage is when the fallback matters most, and taking the reasoner
        # out of rotation then would leave incidents with no analysis at all.
        if database is None or not await database.ping():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": "database unreachable"}
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = render_latest(metrics_registry)
        return Response(content=payload, media_type=content_type)

    @app.middleware("http")
    async def record_request_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            request_metrics.errors_total.labels(
                settings.service_name, type(exc).__name__
            ).inc()
            request_metrics.requests_total.labels(
                settings.service_name, request.url.path, "500"
            ).inc()
            raise
        request_metrics.requests_total.labels(
            settings.service_name, request.url.path, str(response.status_code)
        ).inc()
        request_metrics.request_duration_seconds.labels(
            settings.service_name, request.url.path
        ).observe(time.perf_counter() - started)
        if response.status_code >= 500:
            request_metrics.errors_total.labels(
                settings.service_name, f"http_{response.status_code}"
            ).inc()
        return response

    if with_tracing:
        setup_tracing(service_name=settings.service_name)
        instrument_fastapi(app)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Build the ASGI ``app`` lazily on first access (``main:app`` for uvicorn).

    Importing this module — e.g. to reach :func:`create_app` from tests — must have no
    side effects; in particular it must not register platform metrics on the global
    Prometheus registry, which would collide when another service's app is imported in
    the same process. That is the import-time collision Phase 5 hit with an eager
    ``app = create_app()``. Cached, so repeated ``app`` access returns one instance.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
