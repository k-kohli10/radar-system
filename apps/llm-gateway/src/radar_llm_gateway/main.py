"""llm-gateway service assembly.

Startup contract (all of it inside the lifespan, none at import time):

1. ``bootstrap(GatewaySettings, with_agent_auth=False)`` — the gateway has no
   single inbound agent token, so ``bootstrap`` returns ``auth=None`` **by
   design**. That is handled explicitly here and auth is NOT skipped: the
   gateway enforces caller auth itself via :class:`GatewayAuth` over the
   token→mode map (``core/security.py``), built during startup.
2. Plugin registration, config load, token-map load, and router construction
   all run **inside the lifespan startup block** — never at module import —
   so any failure (plugin conformance, bad config, missing Vault secret or
   API key) is caught, keeps :class:`Readiness` false, and surfaces as a
   ``/readyz`` 503 instead of an import-time crash the probe never sees.
3. Only after everything above succeeds are the ``/v1`` routers mounted and
   readiness marked true. While not ready, ``/v1/*`` does not exist (404);
   Kubernetes routes no traffic to a pod whose readyz is 503.

Shutdown contract: the FIRST action on the shutdown path is
``readiness.mark_not_ready("shutting down")`` — before any draining — so a
load balancer health-checking the pod during shutdown sees 503 immediately
and stops sending traffic.

Also wired here: the app-level error handlers (AllProvidersFailedError ->
503), the 401-beats-422 validation handler, the platform request metrics
middleware (``radar_requests_total``/``duration``/``errors_total``), OTel
FastAPI instrumentation, and ``/healthz``, ``/readyz``, ``/metrics``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import radar_plugin_llm_anthropic
import radar_plugin_llm_gemini
import radar_plugin_llm_openai
from fastapi import FastAPI, Request, Response
from prometheus_client import REGISTRY, CollectorRegistry
from radar_common import ConfigurationError, bootstrap
from radar_contracts import EmbeddingProvider, LLMProvider
from radar_plugin_sdk import PluginRegistry
from radar_telemetry import (
    create_llm_metrics,
    create_request_metrics,
    instrument_fastapi,
    setup_tracing,
)

from radar_llm_gateway.api.chat import create_chat_router
from radar_llm_gateway.api.embed import create_embed_router
from radar_llm_gateway.api.health import Readiness, create_health_router
from radar_llm_gateway.api.metrics import create_metrics_router
from radar_llm_gateway.core.config import (
    GatewaySettings,
    load_gateway_config,
    load_token_map,
)
from radar_llm_gateway.core.errors import install_error_handlers
from radar_llm_gateway.core.security import (
    GatewayAuth,
    install_guarded_validation_handler,
)
from radar_llm_gateway.gateway.model_router import build_router
from radar_llm_gateway.gateway.service import GatewayService


def register_plugins(registry: PluginRegistry) -> None:
    """Register every LLM provider plugin the gateway can route to.

    Called from lifespan startup only: a conformance failure here must keep
    readiness false, not crash the module import.
    """
    registry.register(
        LLMProvider,
        radar_plugin_llm_openai.OpenAIChatProvider,
        name=radar_plugin_llm_openai.PROVIDER,
    )
    registry.register(
        EmbeddingProvider,
        radar_plugin_llm_openai.OpenAIEmbeddingProvider,
        name=radar_plugin_llm_openai.PROVIDER,
    )
    registry.register(
        LLMProvider,
        radar_plugin_llm_anthropic.AnthropicChatProvider,
        name=radar_plugin_llm_anthropic.PROVIDER,
    )
    registry.register(
        LLMProvider,
        radar_plugin_llm_gemini.GeminiChatProvider,
        name=radar_plugin_llm_gemini.PROVIDER,
    )
    registry.register(
        EmbeddingProvider,
        radar_plugin_llm_gemini.GeminiEmbeddingProvider,
        name=radar_plugin_llm_gemini.PROVIDER,
    )


def create_app(
    *,
    metrics_registry: CollectorRegistry = REGISTRY,
    with_tracing: bool = True,
) -> FastAPI:
    """Build the gateway app. ``metrics_registry`` is injectable for tests."""
    runtime = bootstrap(GatewaySettings, with_agent_auth=False)
    # bootstrap(with_agent_auth=False) returns auth=None by design: the
    # gateway validates callers' tokens against its own token→mode map. Fail
    # loudly if that assumption ever changes rather than half-using it.
    if runtime.auth is not None:
        raise ConfigurationError(
            "gateway bootstrap unexpectedly built an AgentTokenAuth; "
            "the gateway must enforce auth via its own token map"
        )
    settings = runtime.settings
    log = runtime.log

    readiness = Readiness()
    llm_metrics = create_llm_metrics(metrics_registry)
    request_metrics = create_request_metrics(metrics_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            registry = PluginRegistry()
            register_plugins(registry)
            config = load_gateway_config(settings.gateway_config_path)
            token_map = load_token_map()
            router = build_router(config, registry)
            auth = GatewayAuth(token_map)
            service = GatewayService(config=config, router=router, metrics=llm_metrics)
            if not getattr(app.state, "v1_mounted", False):
                install_guarded_validation_handler(app, auth)
                app.include_router(create_chat_router(service, auth))
                app.include_router(create_embed_router(service, auth))
                app.state.v1_mounted = True
            readiness.mark_ready()
            log.info(
                "gateway.ready",
                modes={
                    mode.value: f"{mc.provider}/{mc.model}"
                    for mode, mc in config.modes.items()
                },
                fallback_modes=[mode.value for mode in config.fallback],
                token_entries=len(token_map),
            )
        except ConfigurationError as exc:
            # Config-layer messages are written to be secret-free.
            readiness.mark_not_ready(str(exc))
            log.error("gateway.startup_failed", reason=str(exc))
        except Exception as exc:
            # Unexpected failure: expose the class only, never the message —
            # arbitrary exception text is not vetted against the logging policy.
            readiness.mark_not_ready(f"startup failed: {type(exc).__name__}")
            log.error("gateway.startup_failed", error_type=type(exc).__name__)
        try:
            yield
        finally:
            # FIRST action on shutdown, before any draining: a load balancer
            # probing readyz mid-shutdown must see 503, not 200, or it will
            # keep sending traffic to a dying pod.
            readiness.mark_not_ready("shutting down")
            log.info("gateway.shutdown")

    app = FastAPI(title="radar-llm-gateway", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(create_health_router(readiness))
    app.include_router(create_metrics_router(metrics_registry))

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


app = create_app()
