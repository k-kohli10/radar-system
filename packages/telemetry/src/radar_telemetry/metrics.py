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
            "Total recommendations produced by template fallback.",
            registry=registry,
        ),
        feedback_total=Counter(
            "radar_feedback_total",
            "Total feedback submissions by sentiment.",
            ["sentiment"],
            registry=registry,
        ),
    )
