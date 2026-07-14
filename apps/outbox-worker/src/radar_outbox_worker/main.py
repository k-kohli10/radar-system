"""outbox-worker service assembly.

The outbox worker is a background processor with a thin HTTP surface. Startup
(all inside the lifespan, nothing at import time) reads the Postgres DSN and the
worker's own ``agent_token`` from Vault, builds the :class:`~radar_database.Database`
and a shared ``httpx.AsyncClient``, and launches two background tasks — the
:class:`~radar_outbox_worker.poller.Poller` (claim + dispatch) and the
:class:`~radar_outbox_worker.dead_letter.Reaper` (recover stuck rows). If a secret
is missing, readiness stays false and ``/readyz`` answers 503 instead of crashing
the import the probe never sees.

Two distinct sets of credentials, and conflating them is the bug this service is
most likely to have: the worker's OWN ``agent_token`` validates callers of its
admin dead-letter endpoints (inbound), while the ``dispatch_tokens`` map holds each
target's token, which the dispatcher sends *to that target* (outbound). Each agent
has its own token, so there is no one credential that opens them all — the worker
presents the target's, never its own. Both are Vault secrets; a missing either
keeps ``/readyz`` at 503. This worker has NO ``POST /events`` — it is a consumer,
not an agent endpoint; only ``/healthz``, ``/readyz``, ``/metrics``, and
``/admin/*`` are exposed here.

Graceful shutdown (the phase contract: SIGTERM → stop polling → wait ≤30s for
in-flight → exit): uvicorn turns SIGTERM into a lifespan shutdown, which marks
not-ready first (so a probe mid-shutdown sees 503), signals both tasks to stop,
and awaits them within a 30s drain budget — cancelling only if they overrun.
Because the poller checks the stop signal between events and leaves any un-started
tail in ``processing`` for the reaper, the drain bounds to a single in-flight
dispatch, well under budget.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, Request, Response, status
from prometheus_client import REGISTRY, CollectorRegistry
from radar_common import (
    REASONER_DISPATCH_TIMEOUT_SECONDS,
    AgentTokenAuth,
    ConfigurationError,
    RadarSettings,
    bootstrap,
    read_secret,
)
from radar_common.bootstrap import AGENT_TOKEN_SECRET
from radar_database import Database, OutboxEvent
from radar_telemetry import (
    create_outbox_metrics,
    create_request_metrics,
    instrument_fastapi,
    render_latest,
    setup_tracing,
)

from radar_outbox_worker.dead_letter import (
    DEFAULT_REAPER_INTERVAL_SECONDS,
    Reaper,
    list_dead_letters,
    requeue_dead_letter,
)
from radar_outbox_worker.dispatcher import (
    EventDispatcher,
    TargetResolver,
    TimeoutPolicy,
)
from radar_outbox_worker.poller import Poller
from radar_outbox_worker.retry import DispatchProcessor
from radar_outbox_worker.security import AdminAuth, load_dispatch_tokens

DEAD_LETTER_LIST_MAX = 200
DEAD_LETTER_LIST_DEFAULT = 50

#: Max time the shutdown drain waits for in-flight work before cancelling.
DRAIN_TIMEOUT_SECONDS = 30.0

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Raises ``SecretNotFoundError`` when the file is absent,
    failing startup loudly so ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()


class OutboxWorkerSettings(RadarSettings):
    """Non-secret worker settings, read from ``RADAR_*`` env vars."""

    service_name: str = "outbox-worker"
    reaper_interval_seconds: int = DEFAULT_REAPER_INTERVAL_SECONDS
    #: k8s FQDN template; ``{service}`` is filled with the event's target_service.
    dispatch_url_template: str = "http://{service}.radar.svc.cluster.local:8080/events"
    #: Per-target URL overrides (JSON in the env var), for dev/compose/tests.
    dispatch_url_overrides: dict[str, str] = {}
    #: Per-target dispatch timeouts, in seconds. Config, not a special case in code:
    #: the reasoner needs far longer than 10s because it calls an LLM before it can
    #: answer, and the value MUST exceed the reasoner's own LLM budget or the worker
    #: gives up mid-call and redelivers, paying OpenAI twice (radar_common.timeouts).
    #: Defaulted here so a deployment that forgets to set it still works.
    dispatch_timeout_overrides: dict[str, float] = {
        "reasoner-agent": REASONER_DISPATCH_TIMEOUT_SECONDS
    }


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``.

    Starts not-ready; :meth:`mark_ready` flips it once the Vault secrets load and
    the background tasks start. The live database check is separate (``/readyz``
    pings on each call). ``reason`` strings are probe-safe (no secret values).
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


def _dead_letter_view(event: OutboxEvent) -> dict[str, Any]:
    """Serialize a dead-lettered event for the admin list response (no payload)."""
    return {
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "target_service": event.target_service,
        "correlation_id": str(event.correlation_id),
        "attempts": event.attempts,
        "last_error": event.last_error,
        "created_at": event.created_at.isoformat(),
        "updated_at": event.updated_at.isoformat(),
    }


def create_app(
    *,
    metrics_registry: CollectorRegistry = REGISTRY,
    with_tracing: bool = True,
) -> FastAPI:
    """Build the outbox-worker app. ``metrics_registry`` is injectable for tests."""
    runtime = bootstrap(OutboxWorkerSettings, with_agent_auth=False)
    # The worker loads its agent_token itself (below): it needs the raw value for
    # outbound dispatch, which bootstrap's AgentTokenAuth does not expose.
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    request_metrics = create_request_metrics(metrics_registry)
    outbox_metrics = create_outbox_metrics(metrics_registry)
    database: Database | None = None
    # The worker's own agent token guards the admin endpoints inbound. Loaded in
    # the lifespan (same secret used outbound by the dispatcher), so the auth
    # dependency late-binds it via the AdminAuth accessor below.
    agent_auth: AgentTokenAuth | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal database, agent_auth
        client: httpx.AsyncClient | None = None
        poller: Poller | None = None
        reaper: Reaper | None = None
        tasks: list[asyncio.Task[None]] = []
        try:
            dsn = load_postgres_dsn()
            agent_token = read_secret(AGENT_TOKEN_SECRET)
            assert agent_token is not None  # required=True: raised if absent
            agent_auth = AgentTokenAuth([agent_token])
            # The worker's OWN token guards its admin endpoints (above); the tokens
            # it SENDS are the targets' (below). Two different things, and the whole
            # point of per-service tokens is that they are not interchangeable.
            dispatch_tokens = load_dispatch_tokens()
            database = Database(dsn)
            client = httpx.AsyncClient()
            timeouts = TimeoutPolicy(overrides=settings.dispatch_timeout_overrides)
            dispatcher = EventDispatcher(
                client,
                TargetResolver(
                    url_template=settings.dispatch_url_template,
                    overrides=settings.dispatch_url_overrides,
                ),
                dispatch_tokens,
                timeouts=timeouts,
                metrics=outbox_metrics,
            )
            poller = Poller(
                database,
                DispatchProcessor(database, dispatcher, metrics=outbox_metrics),
                metrics=outbox_metrics,
            )
            reaper = Reaper(
                database,
                interval_seconds=settings.reaper_interval_seconds,
                metrics=outbox_metrics,
            )
            tasks.append(asyncio.create_task(poller.run()))
            tasks.append(asyncio.create_task(reaper.run()))
            readiness.mark_ready()
            log.info(
                "outbox_worker.ready",
                reaper_interval_seconds=settings.reaper_interval_seconds,
                # Target NAMES only — the map never logs a token value.
                dispatch_targets=dispatch_tokens.targets,
                # Which targets get a non-default budget, out loud. A reasoner
                # missing from this map would time out at 10s mid-LLM-call.
                dispatch_timeouts=timeouts.targets,
            )
        except ConfigurationError as exc:
            # Config-layer messages are written to be secret-free.
            readiness.mark_not_ready(str(exc))
            log.error("outbox_worker.startup_failed", reason=str(exc))
        except Exception as exc:
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("outbox_worker.startup_failed", error_type=type(exc).__name__)

        try:
            yield
        finally:
            # Mark not-ready FIRST so a probe mid-shutdown sees 503, then drain.
            readiness.mark_not_ready("shutting down")
            if poller is not None:
                poller.stop()
            if reaper is not None:
                reaper.stop()
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks), timeout=DRAIN_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    log.warning("outbox_worker.drain_timeout")
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if client is not None:
                await client.aclose()
            if database is not None:
                await database.dispose()
            log.info("outbox_worker.shutdown")

    app = FastAPI(title="radar-outbox-worker", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        if database is None or not await database.ping():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": "database unreachable"}
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        payload, content_type = render_latest(metrics_registry)
        return Response(content=payload, media_type=content_type)

    require_agent_token = AdminAuth(lambda: agent_auth).require()

    @app.get("/admin/dead-letters", dependencies=[Depends(require_agent_token)])
    async def list_dead_letters_endpoint(
        response: Response,
        limit: int = DEAD_LETTER_LIST_DEFAULT,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, DEAD_LETTER_LIST_MAX))
        offset = max(0, offset)
        if database is None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"detail": "database unavailable"}
        async with database.session() as session:
            rows = await list_dead_letters(session, limit=limit, offset=offset)
        return {
            "limit": limit,
            "offset": offset,
            "count": len(rows),
            "events": [_dead_letter_view(row) for row in rows],
        }

    @app.post(
        "/admin/dead-letters/{event_id}/requeue",
        dependencies=[Depends(require_agent_token)],
    )
    async def requeue_endpoint(event_id: UUID, response: Response) -> dict[str, Any]:
        if database is None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"detail": "database unavailable"}
        async with database.session() as session:
            event = await requeue_dead_letter(session, event_id)
            if event is None:
                response.status_code = status.HTTP_404_NOT_FOUND
                return {"detail": "no dead-letter event with that id"}
            view = {
                "status": "requeued",
                "event_id": str(event.event_id),
                "target_service": event.target_service,
            }
            await session.commit()
        return view

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

    Importing this module (e.g. from tests) must have no side effects; in
    particular it must not register platform metrics on the global Prometheus
    registry at import time, which would collide when another service's app is
    imported in the same process. Cached so repeated ``app`` access returns one
    instance, never a second registration.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
