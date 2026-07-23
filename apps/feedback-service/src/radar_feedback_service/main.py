"""feedback-service assembly.

The last stage of the incident pipeline: it consumes ``recommendation.created``
and puts the RCA in front of a human. Everything it needs is loaded inside the
lifespan — nothing at import time: the Postgres DSN, this service's own
``agent_token``, and the Slack bot token all come from Vault, and a missing secret
leaves readiness false so ``/readyz`` answers 503 instead of crashing an import no
probe will ever see. ``Database`` construction is lazy (no connection yet), so a
database that is down at startup does not fail startup — ``/readyz`` live-pings it
and recovers when it returns.

``/readyz`` is 200 only when BOTH hold, every time it is asked:

1. the Vault secrets loaded at startup, AND
2. the database answers ``SELECT 1`` **right now**.

The second is not redundant. A probe that checks only its own startup state
reports healthy while its database is unreachable — Kubernetes keeps routing
traffic to a pod that cannot do anything, and the failure surfaces as errors to
callers instead of a pod taken out of rotation. So the DB is pinged per request,
not remembered from boot.

``/healthz`` is process liveness only — deliberately NOT gated on the database,
or a database blip would get the pod killed and restarted rather than merely
removed from service. Liveness and readiness answer different questions.

**The Slack bot token is required to become ready**, even though this commit does
not yet post anything. A feedback-service without it cannot do the one thing it
exists for, and finding that out at the first real incident — with an engineer
waiting for a card that will never arrive — is strictly worse than refusing to go
ready. Same reasoning as the planner refusing to start without its templates. The
token is loaded and held here; the commit that delivers cards constructs the Slack
backend from it.

The agent token is loaded in the lifespan rather than via
``bootstrap(with_agent_auth=True)``, which would read it at app-build time and
crash on a missing secret — the exact crash ``/readyz`` exists to turn into a
503. It is late-bound into the shared :class:`~radar_common.EventsAuth`, so a
missing token degrades to 503 (which the outbox worker retries) rather than 401
(which it would dead-letter).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from prometheus_client import REGISTRY, CollectorRegistry
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

from radar_feedback_service.config import (
    SERVICE_NAME,
    FeedbackSettings,
    load_postgres_dsn,
    load_slack_bot_token,
)
from radar_feedback_service.routes import create_events_router


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``.

    Starts not-ready ("starting"); :meth:`mark_ready` flips it once the Vault
    secrets have loaded. The live database check is separate (``/readyz`` pings on
    each call), so this tracks only the startup half of the contract. ``reason``
    strings are safe to return to a probe — the config layer names files, never
    secret values.
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
    """Build the feedback-service app. ``metrics_registry`` is injectable for tests."""
    # with_agent_auth=False: bootstrap would read the token at app-build time and
    # raise on a missing secret, which is exactly the crash /readyz exists to turn
    # into a 503. The token is loaded in the lifespan and late-bound below.
    runtime = bootstrap(FeedbackSettings, with_agent_auth=False)
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    request_metrics = create_request_metrics(metrics_registry)
    database: Database | None = None
    agent_auth: AgentTokenAuth | None = None
    slack_bot_token: str | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal database, agent_auth, slack_bot_token
        try:
            dsn = load_postgres_dsn()
            agent_token = read_secret(AGENT_TOKEN_SECRET)
            assert agent_token is not None  # required=True: raised if absent
            agent_auth = AgentTokenAuth([agent_token])
            # Required to go ready — see the module docstring. Held for the
            # delivery commit; nothing posts to Slack yet.
            slack_bot_token = load_slack_bot_token()
            database = Database(dsn)
            readiness.mark_ready()
            log.info(
                "feedback.ready",
                # The channel, out loud. It is deployment config, and a card
                # delivered to the wrong channel looks identical to no card at all
                # from the on-call engineer's side — this is the line that says
                # where they will actually land.
                slack_channel=settings.slack_channel,
                slack_bot_name=settings.slack_bot_name,
            )
        except ConfigurationError as exc:
            # Config-layer messages are written to be secret-free.
            readiness.mark_not_ready(str(exc))
            log.error("feedback.startup_failed", reason=str(exc))
        except Exception as exc:
            # Unexpected failure: expose the class only, never the message.
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("feedback.startup_failed", error_type=type(exc).__name__)
        try:
            yield
        finally:
            # FIRST on shutdown, before draining: a probe mid-shutdown must see
            # 503, not 200, or traffic keeps arriving at a dying pod.
            readiness.mark_not_ready("shutting down")
            if database is not None:
                await database.dispose()
            log.info("feedback.shutdown")

    def get_database() -> Database | None:
        # Read per request: None until startup sets it (or if startup failed), so
        # the handler answers 503 rather than touching a missing database.
        return database

    def get_agent_auth() -> AgentTokenAuth | None:
        # Late-bound: None until the Vault secret loads, so the auth dependency
        # answers 503 while not ready and 401 once it is.
        return agent_auth

    events_auth = EventsAuth(get_agent_auth, service_name=SERVICE_NAME)

    app = FastAPI(title="radar-feedback-service", lifespan=lifespan)
    # Make 401 beat 422 on /events: a malformed body must not mask a bad token,
    # and an unauthenticated caller must not learn the shape of the contract.
    install_guarded_events_handler(app, events_auth)
    app.include_router(
        create_events_router(get_database=get_database, events_auth=events_auth)
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only. NOT gated on the database: a DB blip should take the pod
        # out of rotation (readyz), not have it killed and restarted (healthz).
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        # Secrets loaded; now the live half of the contract. Asked every time,
        # because a probe that trusts its boot-time state will report healthy with
        # a dead database underneath it.
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

    Importing this module — e.g. to reach :func:`create_app` from tests — must
    have no side effects; in particular it must not register platform metrics on
    the global Prometheus registry, which would collide (`Duplicated timeseries`)
    when another service's app is imported in the same process.
    Cached, so repeated ``app`` access returns one instance and never a second
    registration.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
