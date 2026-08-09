"""Every pipeline hop's SERVER span must carry correlation_id — proven, per hop.

Phase 10, step 3. This harness was written to make the per-hop span coverage
verifiable, and in doing so it caught a real bug: all five services called
``radar_common.bind_correlation_id`` (log context only) instead of
``radar_telemetry.bind_correlation_id`` (which also stamps the current span). So
correlation_id rode the logs but never the traces, and an incident would have
been un-findable in Kibana APM by correlation_id alone — the exact Phase 10
done-condition. The fix — switching the import in each service, plus renaming the
log-only helper to ``bind_log_correlation_id`` to kill the name collision — landed
in its own commits; this test is what turns the guarantee red if it regresses.

For each hop it mounts the service's REAL router (the real ``bind_correlation_id``
call) on a fresh app whose tracing is wired to an in-memory exporter, POSTs
through it with a valid token and NO database wired, and asserts the exported
SERVER span carries correlation_id. Because ``bind`` runs before the readiness
check, the request 503s (or, for ingestion, 422s on the empty body) and the span
is still stamped — so no real Postgres is needed. Parametrized with explicit
per-hop ids, so a failure names the hop that lost its span.

Teeth: this test was red on the pre-fix code — all five hops — and green after;
the fix is what flips it. Reverting any one service's import back to the log-only
bind turns exactly that hop's case red again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from prometheus_client import CollectorRegistry
from radar_common import (
    AGENT_TOKEN_HEADER,
    CORRELATION_ID_KEY,
    AgentTokenAuth,
    EventsAuth,
)

# Router factories — the real handlers, imported per service.
from radar_feedback_service.routes import create_events_router as feedback_router
from radar_ingestion.normalizer import AlertSource
from radar_ingestion.routes import create_alerts_router
from radar_ingestion.security import WEBHOOK_TOKEN_HEADER, WebhookAuth, WebhookTokenMap
from radar_planner_agent.routes import create_events_router as planner_router
from radar_reasoner_agent.routes import create_events_router as reasoner_router
from radar_telemetry import (
    create_ingestion_metrics,
    create_planner_metrics,
    create_reasoner_metrics,
    instrument_fastapi,
    setup_tracing,
)
from radar_watcher_agent.routes import create_events_router as watcher_router

TOKEN = "t" * 64


@dataclass
class Hop:
    """One pipeline hop's request: how to build its router and drive it."""

    router: APIRouter
    endpoint: str
    headers: dict[str, str]
    body: dict[str, Any]
    #: The correlation id the span must carry, or None when the hop MINTS it
    #: (ingestion), in which case any valid uuid on the span is acceptable.
    correlation_id: str | None


def _events_hop(make_router: Callable[..., APIRouter], **deps: Any) -> Hop:
    """An agent /events hop: real router with stub deps, valid token, no DB.

    ``get_database`` returns None so the handler answers 503 — but only AFTER
    ``bind_correlation_id`` has stamped the SERVER span, which is the point.
    """
    correlation_id = str(uuid4())
    auth = AgentTokenAuth([TOKEN])
    events_auth = EventsAuth(lambda: auth, service_name="hop-test")
    router = make_router(get_database=lambda: None, events_auth=events_auth, **deps)
    body = {
        "event_id": str(uuid4()),
        "event_type": "alert.normalized",
        "correlation_id": correlation_id,
        "payload": {},
    }
    return Hop(router, "/events", {AGENT_TOKEN_HEADER: TOKEN}, body, correlation_id)


def _ingestion_hop() -> Hop:
    """Ingestion's /alerts/mock hop. It MINTS the correlation id, so the span
    must carry *a* valid uuid rather than one we supplied."""
    token_map = WebhookTokenMap({AlertSource.MOCK: TOKEN})
    webhook_auth = WebhookAuth(lambda: token_map)
    router = create_alerts_router(
        get_database=lambda: None,
        webhook_auth=webhook_auth,
        metrics=create_ingestion_metrics(CollectorRegistry()),
    )
    return Hop(router, "/alerts/mock", {WEBHOOK_TOKEN_HEADER: TOKEN}, {}, None)


def _hops() -> dict[str, Callable[[], Hop]]:
    """Lazy builders keyed by hop id, so a build failure names its hop."""
    return {
        "ingestion": _ingestion_hop,
        "watcher": lambda: _events_hop(watcher_router, get_rules=lambda: None),
        "planner": lambda: _events_hop(
            planner_router,
            get_templates=lambda: None,
            metrics=create_planner_metrics(CollectorRegistry()),
        ),
        "reasoner": lambda: _events_hop(
            reasoner_router,
            get_gateway=lambda: None,
            get_knowledge=lambda: None,
            metrics=create_reasoner_metrics(CollectorRegistry()),
        ),
        "feedback": lambda: _events_hop(
            feedback_router, get_notifier=lambda: None, channel="hop-test"
        ),
    }


def _instrumented_app(router: APIRouter, exporter: InMemorySpanExporter) -> FastAPI:
    """A fresh app with the router mounted and tracing wired to ``exporter``.

    Uses the real ``setup_tracing``/``instrument_fastapi`` — the same calls a
    service makes when ``with_tracing=True`` — but with an in-memory exporter and
    ``set_global=False`` so parametrized cases stay isolated.
    """
    provider = setup_tracing(
        service_name="hop-test",
        span_processor=SimpleSpanProcessor(exporter),
        set_global=False,
    )
    app = FastAPI()
    app.include_router(router)
    instrument_fastapi(app, tracer_provider=provider)
    return app


@pytest.mark.parametrize("hop_id", list(_hops()))
async def test_pipeline_hop_span_carries_correlation_id(hop_id: str) -> None:
    hop = _hops()[hop_id]()
    exporter = InMemorySpanExporter()
    app = _instrumented_app(hop.router, exporter)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://hop") as client:
        # Status is intentionally unasserted: bind runs before the readiness gate,
        # so whether this 503s (no DB) or 422s (ingestion, empty body) the span is
        # already stamped. What matters is the span, not the response.
        await client.post(hop.endpoint, json=hop.body, headers=hop.headers)

    server_spans = [
        span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
    ]
    assert server_spans, f"{hop_id}: no SERVER span was exported for the request"
    attributes = server_spans[-1].attributes or {}
    got = attributes.get(CORRELATION_ID_KEY)
    assert got is not None, (
        f"{hop_id}: the SERVER span carries no {CORRELATION_ID_KEY!r} attribute — "
        f"the hop would be invisible to a correlation-id trace query"
    )
    if hop.correlation_id is not None:
        assert got == hop.correlation_id, (
            f"{hop_id}: span carries {got!r}, expected the request's "
            f"correlation id {hop.correlation_id!r}"
        )
    else:
        # Minted at ingress — must at least be a real uuid, not an empty marker.
        UUID(str(got))
