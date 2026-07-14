"""Prometheus metric factories.

The plan defines four metric families (see the Observability section). Each
factory below builds one family and returns a frozen bundle of the metric
objects; a service calls the factories it needs once at startup and renders them
at ``/metrics`` with :func:`render_latest`.

Factories take a ``registry`` (default: the global ``REGISTRY``) so tests can
pass a fresh ``CollectorRegistry`` and avoid duplicate-registration errors.
Metric names are spelled exactly as the plan lists them; ``prometheus_client``
exposes counters ending in ``_total`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


def render_latest(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """Render ``registry`` in Prometheus text format for a ``/metrics`` route.

    Returns ``(payload, content_type)`` for the HTTP response.
    """
    return generate_latest(registry), CONTENT_TYPE_LATEST


@dataclass(frozen=True)
class RequestMetrics:
    """Platform-wide HTTP metrics every service exposes."""

    requests_total: Counter
    request_duration_seconds: Histogram
    errors_total: Counter


def create_request_metrics(registry: CollectorRegistry = REGISTRY) -> RequestMetrics:
    return RequestMetrics(
        requests_total=Counter(
            "radar_requests_total",
            "Total HTTP requests handled.",
            ["service", "endpoint", "status_code"],
            registry=registry,
        ),
        request_duration_seconds=Histogram(
            "radar_request_duration_seconds",
            "HTTP request duration in seconds.",
            ["service", "endpoint"],
            registry=registry,
        ),
        errors_total=Counter(
            "radar_errors_total",
            "Total errors by type.",
            ["service", "error_type"],
            registry=registry,
        ),
    )


@dataclass(frozen=True)
class LLMMetrics:
    """LLM gateway metrics."""

    requests_total: Counter
    duration_seconds: Histogram
    time_to_first_token_seconds: Histogram
    tokens_total: Counter
    provider_errors_total: Counter
    fallback_total: Counter
    template_fallback_total: Counter


def create_llm_metrics(registry: CollectorRegistry = REGISTRY) -> LLMMetrics:
    return LLMMetrics(
        requests_total=Counter(
            "radar_llm_requests_total",
            "Total LLM gateway requests.",
            ["mode", "provider", "status"],
            registry=registry,
        ),
        duration_seconds=Histogram(
            "radar_llm_duration_seconds",
            "LLM request duration in seconds.",
            ["mode", "provider"],
            registry=registry,
        ),
        time_to_first_token_seconds=Histogram(
            "radar_llm_time_to_first_token_seconds",
            "Time to first streamed token in seconds.",
            ["mode", "provider"],
            registry=registry,
        ),
        tokens_total=Counter(
            "radar_llm_tokens_total",
            "Total tokens processed, by direction (prompt/completion).",
            ["mode", "provider", "direction"],
            registry=registry,
        ),
        provider_errors_total=Counter(
            "radar_llm_provider_errors_total",
            "Total LLM provider errors.",
            ["mode", "provider", "error"],
            registry=registry,
        ),
        fallback_total=Counter(
            "radar_llm_fallback_total",
            "Total provider-to-provider fallbacks.",
            ["from_provider", "to_provider"],
            registry=registry,
        ),
        template_fallback_total=Counter(
            "radar_llm_template_fallback_total",
            "Total fallbacks to a templated (non-LLM) response.",
            registry=registry,
        ),
    )


@dataclass(frozen=True)
class OutboxMetrics:
    """Transactional outbox worker metrics."""

    depth: Gauge
    processing: Gauge
    dead_letter_total: Counter
    dispatch_duration_seconds: Histogram
    retry_total: Counter


def create_outbox_metrics(registry: CollectorRegistry = REGISTRY) -> OutboxMetrics:
    return OutboxMetrics(
        depth=Gauge(
            "radar_outbox_depth",
            "Number of pending outbox events.",
            registry=registry,
        ),
        processing=Gauge(
            "radar_outbox_processing",
            "Number of outbox events currently being processed.",
            registry=registry,
        ),
        dead_letter_total=Counter(
            "radar_outbox_dead_letter_total",
            "Total events moved to the dead-letter state.",
            registry=registry,
        ),
        dispatch_duration_seconds=Histogram(
            "radar_outbox_dispatch_duration_seconds",
            "Outbox event dispatch duration in seconds.",
            ["target_service"],
            registry=registry,
        ),
        retry_total=Counter(
            "radar_outbox_retry_total",
            "Total outbox dispatch retries.",
            ["event_type"],
            registry=registry,
        ),
    )


@dataclass(frozen=True)
class IncidentMetrics:
    """Incident pipeline business metrics."""

    incidents_total: Counter
    incident_duration_seconds: Histogram
    recommendations_total: Counter
    #: Labelled by ``reason`` — see the factory below. The plan specifies this counter
    #: unlabelled; that would make it unactionable.
    recommendations_fallback_total: Counter
    feedback_total: Counter


def create_incident_metrics(registry: CollectorRegistry = REGISTRY) -> IncidentMetrics:
    return IncidentMetrics(
        incidents_total=Counter(
            "radar_incidents_total",
            "Total incidents opened.",
            ["service", "severity"],
            registry=registry,
        ),
        incident_duration_seconds=Histogram(
            "radar_incident_duration_seconds",
            "Incident open-to-resolution duration in seconds.",
            registry=registry,
        ),
        recommendations_total=Counter(
            "radar_recommendations_total",
            "Total recommendations produced.",
            ["provider", "confidence"],
            registry=registry,
        ),
        recommendations_fallback_total=Counter(
            "radar_recommendations_fallback_total",
            "Total recommendations produced by template fallback, by reason.",
            # WHY THIS COUNTER IS LABELLED WHEN THE PLAN SAYS IT IS NOT
            #
            # A bare total answers "are we falling back?" — which is the question you
            # ask second. The first one is "is this OUR fault?", and it decides who gets
            # woken up:
            #
            #   gateway_unavailable / timeout  -> the LLM is down or slow. RADAR is
            #                                     fine. Wait it out, or page whoever
            #                                     owns the provider.
            #   rejected                       -> OUR misconfiguration: a bad token, a
            #                                     mode this token may not use. It fails
            #                                     identically on EVERY incident until a
            #                                     human fixes the config.
            #   not_json / schema_invalid      -> the model answered and the answer was
            #                                     unusable. A prompt or model-quality
            #                                     problem, not an outage.
            #
            # `rejected` is the one that hides. A steady 100% fallback rate carrying it
            # means the reasoner has NEVER ONCE used the LLM — every incident silently
            # getting a checklist instead of an analysis — while every dashboard shows a
            # healthy service answering 200s. An unlabelled counter cannot tell that
            # apart from a provider having a bad afternoon, so nobody investigates.
            #
            # The label vocabulary is the reasoner's FallbackReason. It is not imported
            # here — telemetry sits below the apps and must not depend on one — so the
            # coupling is by convention, and the reasoner's own tests pin the values.
            ["reason"],
            registry=registry,
        ),
        feedback_total=Counter(
            "radar_feedback_total",
            "Total feedback submissions by sentiment.",
            ["sentiment"],
            registry=registry,
        ),
    )


@dataclass(frozen=True)
class PlannerMetrics:
    """Planner metrics — both of them exist to make a SILENT bug loud.

    ``plans_created_total`` is labelled ``matched`` vs ``default``. The planner
    matches its template exactly, so a key that drifts (a rename on one side, a
    stray space) never matches and every affected alert falls through to the
    generic ``_default`` plan — which looks perfectly plausible. Nothing errors.
    The only symptom is that the default rate climbs. This counter is that symptom,
    on a dashboard, and it is the production analog of the round-trip key test: CI
    catches the drift it can see, this catches the drift it cannot.

    ``duplicate_plan_requests_total`` counts plan_requested events for an incident
    that already has a plan. The planner absorbs these (200, no second plan, no
    second LLM call), which is right — but absorbing an upstream bug in silence is
    how it stays a bug. A non-zero rate here means the watcher is double-emitting.
    """

    plans_created_total: Counter
    duplicate_plan_requests_total: Counter


def create_planner_metrics(registry: CollectorRegistry = REGISTRY) -> PlannerMetrics:
    return PlannerMetrics(
        plans_created_total=Counter(
            "radar_plans_created_total",
            "Investigation plans created, by whether a specific template matched.",
            ["outcome"],
            registry=registry,
        ),
        duplicate_plan_requests_total=Counter(
            "radar_duplicate_plan_requests_total",
            "plan_requested events for an incident that already had a plan.",
            registry=registry,
        ),
    )
