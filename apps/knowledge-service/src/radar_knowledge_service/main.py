"""knowledge-service assembly.

The retrieval side of the knowledge service: the context API the reasoner calls
to ground an RCA. Everything is loaded inside the lifespan — nothing at import
time: the inbound ``agent_token`` and both outbound gateway tokens come from
Vault-mounted files, and a missing one leaves readiness false so ``/readyz``
answers 503 instead of crashing an import no probe will ever see.

READINESS CHECKS THE INDEX CONTRACT, NOT JUST REACHABILITY
-----------------------------------------------------------
The live half of ``/readyz`` calls ``verify_dims`` on every probe: it confirms
Elasticsearch is reachable AND that the index exists at the dimension this
service would embed queries at. A dimension mismatch is a model swap without a
re-index — every query would come back nonsense while every component reported
healthy — so it takes the pod out of rotation rather than degrading silently.

THE GATEWAY IS DELIBERATELY NOT PART OF READINESS, same reasoning as the
reasoner: this service should stay in rotation when the gateway blips, because a
transient embed failure surfaces per-request as 503 on ``/v1/context`` and the
caller degrades. Gating readiness on the gateway would evict this pod exactly
when the reasoner most needs a fast, honest "retrieval unavailable".

401 BEATS 422 ON THE GUARDED PATH
----------------------------------
FastAPI decodes the request body before dependencies run, so malformed JSON
would answer 422 before the token is ever checked — letting an unauthenticated
caller probe the contract's shape. The validation-error handler below returns
401 first when the token is missing or unknown, the same rule the llm-gateway
enforces on its ``/v1/`` paths.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import REGISTRY, CollectorRegistry
from radar_common import (
    AGENT_TOKEN_HEADER,
    AgentTokenAuth,
    ConfigurationError,
    bootstrap,
    read_secret,
)
from radar_common.bootstrap import AGENT_TOKEN_SECRET
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore
from radar_telemetry import (
    create_request_metrics,
    instrument_fastapi,
    render_latest,
    setup_tracing,
)

from radar_knowledge_service.api import create_context_router
from radar_knowledge_service.config import KnowledgeSettings, load_gateway_tokens
from radar_knowledge_service.crag_client import GatewayGrader
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.retrieval import HybridRetriever

GUARDED_PATH_PREFIX = "/v1/"


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``."""

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
    """Build the knowledge-service app. ``metrics_registry`` is injectable."""
    # with_agent_auth=False: bootstrap would read the token at app-build time and
    # raise on a missing secret — the crash /readyz exists to turn into a 503.
    runtime = bootstrap(KnowledgeSettings, with_agent_auth=False)
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    request_metrics = create_request_metrics(metrics_registry)
    store: ElasticKnowledgeStore | None = None
    gateway_client: httpx.AsyncClient | None = None
    agent_auth: AgentTokenAuth | None = None
    retriever: HybridRetriever | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal store, gateway_client, agent_auth, retriever
        try:
            agent_token = read_secret(AGENT_TOKEN_SECRET)
            assert agent_token is not None  # required=True: raised if absent
            embed_token, reason_token = load_gateway_tokens()

            agent_auth = AgentTokenAuth([agent_token])
            store = ElasticKnowledgeStore(
                hosts=settings.elasticsearch_url,
                dims=settings.embedding_dims,
                index=settings.index_name,
            )
            # timeout=None: asyncio.timeout inside each gateway client is the
            # only clock, and both constructors refuse a client that undercuts.
            gateway_client = httpx.AsyncClient(
                base_url=settings.gateway_url, timeout=None
            )
            retriever = HybridRetriever(
                backend=store,
                embedder=GatewayEmbeddingClient(
                    gateway_client, embed_token, dims=settings.embedding_dims
                ),
                grader=GatewayGrader(gateway_client, reason_token),
            )
            readiness.mark_ready()
            log.info(
                "knowledge.ready",
                index=settings.index_name,
                dims=settings.embedding_dims,
                elasticsearch=settings.elasticsearch_url,
                gateway=settings.gateway_url,
            )
        except ConfigurationError as exc:
            readiness.mark_not_ready(str(exc))
            log.error("knowledge.startup_failed", reason=str(exc))
        except Exception as exc:
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("knowledge.startup_failed", error_type=type(exc).__name__)
        try:
            yield
        finally:
            readiness.mark_not_ready("shutting down")
            if store is not None:
                await store.close()
            if gateway_client is not None:
                await gateway_client.aclose()
            log.info("knowledge.shutdown")

    def get_retriever() -> HybridRetriever | None:
        return retriever

    def get_agent_auth() -> AgentTokenAuth | None:
        return agent_auth

    app = FastAPI(title="radar-knowledge-service", lifespan=lifespan)
    app.include_router(
        create_context_router(
            get_retriever=get_retriever, get_agent_auth=get_agent_auth
        )
    )

    @app.exception_handler(RequestValidationError)
    async def _guarded_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # 401 beats 422 on guarded paths — see the module docstring.
        if request.url.path.startswith(GUARDED_PATH_PREFIX):
            token = request.headers.get(AGENT_TOKEN_HEADER)
            if agent_auth is None or not agent_auth.verify(token):
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing agent token"},
                )
        return await request_validation_exception_handler(request, exc)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only — NOT gated on Elasticsearch: an ES blip should remove
        # the pod from rotation (readyz), not have it killed (healthz).
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        # The live half: the index exists, at the dimension we embed at. Asked
        # every time — a probe trusting boot-time state reports healthy over a
        # dead cluster or a swapped model.
        assert store is not None  # mark_ready() implies startup assigned it
        try:
            live = await store.verify_dims()
        except Exception as exc:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "not_ready",
                "reason": f"elasticsearch: {type(exc).__name__}",
            }
        if live != settings.embedding_dims:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "not_ready",
                "reason": (
                    f"index dimension {live} != configured "
                    f"{settings.embedding_dims}; re-index required"
                ),
            }
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

    Importing this module must have no side effects — eager ``create_app()``
    registers platform metrics on the global registry and collides when another
    service's app is imported in the same process (the Phase 5 lesson).
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
