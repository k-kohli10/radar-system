"""ingestion service assembly.

Startup (all inside the lifespan, nothing at import time) reads the Postgres
DSN from Vault and constructs the :class:`~radar_database.Database`. If the DSN
secret is missing, readiness stays false and ``/readyz`` answers 503 instead of
crashing the import the probe never sees. ``Database`` construction is lazy (no
connection yet), so a database that is down at startup does not fail startup —
``/readyz`` live-pings it on every call and recovers once the database is back.

``/readyz`` is 200 only when both hold: the Vault secrets loaded at startup AND
the database answers ``SELECT 1`` right now (the phase contract). ``/healthz``
is process liveness only. Shutdown marks not-ready first (so a load balancer
sees 503 immediately) and then disposes the engine pool.

Ingestion is the entry point, not an agent: ``bootstrap`` is called with
``with_agent_auth=False`` (no inbound agent token; ``/alerts/*`` use a webhook
token wired in a later commit). Also here: the platform request-metrics
middleware (``radar_requests_total``/``duration``/``errors_total``) and OTel
FastAPI instrumentation.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from prometheus_client import REGISTRY, CollectorRegistry
from radar_common import ConfigurationError, bootstrap
from radar_database import Database
from radar_telemetry import (
    create_request_metrics,
    instrument_fastapi,
    render_latest,
    setup_tracing,
)

from radar_ingestion.config import IngestionSettings, load_postgres_dsn
from radar_ingestion.routes import create_alerts_router
from radar_ingestion.security import (
    WebhookAuth,
    WebhookTokenMap,
    install_guarded_webhook_validation_handler,
    load_webhook_tokens,
)


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``.

    Starts not-ready ("starting"); :meth:`mark_ready` flips it once the Vault
    secrets have loaded. The live database check is separate (``/readyz`` pings
    on each call), so this tracks only the startup half of the contract.
    ``reason`` strings are safe to return to a probe — the config layer names
    files, never secret values.
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
    """Build the ingestion app. ``metrics_registry`` is injectable for tests."""
    runtime = bootstrap(IngestionSettings, with_agent_auth=False)
    # Ingestion has no inbound agent token; /alerts/* use a webhook token.
    # bootstrap(with_agent_auth=False) returns auth=None by design — fail loudly
    # if that ever changes rather than half-using it.
    if runtime.auth is not None:
        raise ConfigurationError(
            "ingestion bootstrap unexpectedly built an AgentTokenAuth; "
            "inbound /alerts/* use X-Radar-Webhook-Token, not an agent token"
        )
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    request_metrics = create_request_metrics(metrics_registry)
    database: Database | None = None
    webhook_tokens: WebhookTokenMap | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal database, webhook_tokens
        try:
            dsn = load_postgres_dsn()
            database = Database(dsn)
            webhook_tokens = load_webhook_tokens()
            readiness.mark_ready()
            log.info("ingestion.ready", webhook_sources=webhook_tokens.sources)
        except ConfigurationError as exc:
            # Config-layer messages are written to be secret-free.
            readiness.mark_not_ready(str(exc))
            log.error("ingestion.startup_failed", reason=str(exc))
        except Exception as exc:
            # Unexpected failure: expose the class only, never the message.
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("ingestion.startup_failed", error_type=type(exc).__name__)
        try:
            yield
        finally:
            # FIRST on shutdown, before draining: a probe mid-shutdown must see
            # 503, not 200, or traffic keeps arriving at a dying pod.
            readiness.mark_not_ready("shutting down")
            if database is not None:
                await database.dispose()
            log.info("ingestion.shutdown")

    def get_database() -> Database | None:
        # Reads the current value each request: None until startup sets it (or
        # if startup failed), so the alert routes can answer 503 instead of
        # touching a missing database.
        return database

    def get_webhook_tokens() -> WebhookTokenMap | None:
        # Same late-binding as get_database: None until startup loads the Vault
        # secret, so the webhook auth dependency answers 503 while not ready.
        return webhook_tokens

    webhook_auth = WebhookAuth(get_webhook_tokens)

    app = FastAPI(title="radar-ingestion", lifespan=lifespan)
    # Make 401 beat 422 on /alerts/*: a malformed body must not mask a bad token.
    install_guarded_webhook_validation_handler(app, webhook_auth)
    app.include_router(
        create_alerts_router(get_database=get_database, webhook_auth=webhook_auth)
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        # Secrets loaded; now the live half of the contract: DB reachable now.
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
    when another service's app is imported in the same process. Cached so
    repeated ``app`` access returns one instance, never a second registration.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
