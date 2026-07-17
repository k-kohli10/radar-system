"""Schema validation and serialization tests for radar_contracts models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from radar_contracts import (
    BotCommand,
    BotCommandType,
    BotResponse,
    Confidence,
    EventEnvelope,
    FeedbackEvent,
    GatewayStreamEvent,
    Incident,
    InvestigationPlan,
    LLMMode,
    LLMRequest,
    LLMResponse,
    Message,
    NormalizedAlert,
    OutboxEvent,
    PlanStep,
    ProcessedEvent,
    Recommendation,
    RecommendationCreatedPayload,
    RecommendedAction,
    Usage,
)


def _alert() -> NormalizedAlert:
    return NormalizedAlert(
        source="prometheus",
        fingerprint="fp",
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
        severity="critical",
        raw_payload={"k": "v"},
        fired_at=datetime.now(UTC),
    )


def _recommendation(
    *,
    confidence: Confidence = Confidence.MEDIUM,
    prompt_tokens: int | None = None,
) -> Recommendation:
    return Recommendation(
        incident_id=uuid4(),
        plan_id=uuid4(),
        correlation_id=uuid4(),
        llm_provider="openai",
        model_alias="extended",
        model_id="gpt-4o",
        root_cause="bad deploy",
        confidence=confidence,
        recommended_actions=[RecommendedAction(order=1, action="rollback")],
        prompt_tokens=prompt_tokens,
    )


# --- alerts -----------------------------------------------------------------


def test_normalized_alert_defaults() -> None:
    alert = _alert()
    assert alert.status == "firing"
    assert isinstance(alert.id, UUID)
    assert alert.labels == {}
    assert alert.annotations == {}
    assert alert.resolved_at is None


def test_normalized_alert_forbids_extra_fields() -> None:
    payload: dict[str, Any] = {
        "source": "prometheus",
        "fingerprint": "fp",
        "service_name": "s",
        "alert_name": "n",
        "severity": "critical",
        "raw_payload": {},
        "fired_at": datetime.now(UTC),
        "unexpected": "boom",
    }
    with pytest.raises(ValidationError):
        NormalizedAlert.model_validate(payload)


def test_normalized_alert_serialization_roundtrip() -> None:
    alert = _alert()
    restored = NormalizedAlert.model_validate_json(alert.model_dump_json())
    assert restored == alert


def test_alert_default_timestamp_is_timezone_aware() -> None:
    assert _alert().received_at.tzinfo is not None


def test_alert_requires_fired_at() -> None:
    payload: dict[str, Any] = {
        "source": "prometheus",
        "fingerprint": "fp",
        "service_name": "s",
        "alert_name": "n",
        "severity": "critical",
        "raw_payload": {},
    }
    with pytest.raises(ValidationError):
        NormalizedAlert.model_validate(payload)


# --- incidents / plans / recommendations ------------------------------------


def test_incident_defaults() -> None:
    incident = Incident(
        fingerprint="fp",
        service_name="order-service",
        title="t",
        severity="critical",
    )
    assert incident.status == "open"
    assert incident.alert_count == 1
    assert incident.resolved_at is None
    assert incident.closed_at is None


def test_incident_alert_count_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Incident.model_validate(
            {
                "fingerprint": "fp",
                "service_name": "s",
                "title": "t",
                "severity": "critical",
                "alert_count": 0,
            }
        )


def test_plan_step_order_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        PlanStep.model_validate({"order": 0, "description": "x"})


def test_investigation_plan_holds_typed_steps() -> None:
    plan = InvestigationPlan(
        incident_id=uuid4(),
        correlation_id=uuid4(),
        steps=[PlanStep(order=1, description="check deploys")],
    )
    assert plan.status == "pending"
    assert plan.steps[0].order == 1


def test_recommendation_coerces_confidence_string() -> None:
    data = _recommendation().model_dump()
    data["confidence"] = "high"
    assert Recommendation.model_validate(data).confidence is Confidence.HIGH


def test_recommendation_rejects_unknown_confidence() -> None:
    data = _recommendation().model_dump()
    data["confidence"] = "definitely"
    with pytest.raises(ValidationError):
        Recommendation.model_validate(data)


def test_recommendation_token_counts_non_negative() -> None:
    with pytest.raises(ValidationError):
        _recommendation(prompt_tokens=-1)


def test_recommendation_is_fallback_defaults_false() -> None:
    rec = _recommendation()
    assert rec.is_fallback is False
    assert rec.context_bundle == {}
    assert rec.raw_llm_response is None


def test_recommendation_roundtrip_preserves_enum() -> None:
    rec = _recommendation(confidence=Confidence.LOW)
    restored = Recommendation.model_validate_json(rec.model_dump_json())
    assert restored.confidence is Confidence.LOW


# --- events -----------------------------------------------------------------


def test_outbox_event_requires_correlation_id() -> None:
    with pytest.raises(ValidationError):
        OutboxEvent.model_validate(
            {
                "event_type": "alert.normalized",
                "target_service": "watcher-agent",
                "payload": {},
            }
        )


def test_outbox_event_row_id_distinct_from_event_id() -> None:
    event = OutboxEvent(
        event_type="alert.normalized",
        target_service="watcher-agent",
        payload={"x": 1},
        correlation_id=uuid4(),
    )
    assert event.id != event.event_id
    assert event.status == "pending"
    assert event.attempts == 0


def test_processed_event_roundtrip() -> None:
    event = ProcessedEvent(event_id=uuid4(), processed_by="planner-agent")
    restored = ProcessedEvent.model_validate_json(event.model_dump_json())
    assert restored == event


# --- llm --------------------------------------------------------------------


def test_llm_request_mode_enum_and_stream_default() -> None:
    req = LLMRequest(
        mode=LLMMode.EXTENDED, messages=[Message(role="user", content="hi")]
    )
    assert req.mode is LLMMode.EXTENDED
    assert req.stream is False


def test_llm_request_coerces_mode_string() -> None:
    req = LLMRequest.model_validate({"mode": "fast", "messages": []})
    assert req.mode is LLMMode.FAST


def test_llm_request_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        LLMRequest.model_validate({"mode": "turbo", "messages": []})


def test_llm_response_roundtrip() -> None:
    resp = LLMResponse(
        id="resp_1",
        mode=LLMMode.EXTENDED,
        provider="openai",
        model="gpt-4o",
        content="ok",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
        latency_ms=120,
    )
    assert LLMResponse.model_validate_json(resp.model_dump_json()) == resp


def test_usage_completion_tokens_optional() -> None:
    assert Usage(prompt_tokens=8).completion_tokens is None


def test_gateway_stream_event_defaults() -> None:
    event = GatewayStreamEvent()
    assert event.delta == ""
    assert event.done is False
    assert event.usage is None


# --- bot / feedback ---------------------------------------------------------


def test_bot_command_type_enum() -> None:
    cmd = BotCommand(command=BotCommandType.LAST, raw_text="last 5", count=5)
    assert cmd.command is BotCommandType.LAST


def test_bot_command_rejects_unknown_verb() -> None:
    with pytest.raises(ValidationError):
        BotCommand.model_validate({"command": "reboot", "raw_text": "reboot"})


def test_bot_response_defaults() -> None:
    resp = BotResponse(text="3 open incidents")
    assert resp.ephemeral is True
    assert resp.blocks is None


def test_feedback_event_optional_fields_default_none() -> None:
    fb = FeedbackEvent(
        recommendation_id=uuid4(),
        incident_id=uuid4(),
        correlation_id=uuid4(),
        sentiment="helpful",
        llm_provider="openai",
        model_alias="extended",
    )
    assert fb.correction_text is None
    assert fb.slack_user_id is None
    assert fb.slack_message_ts is None


def test_event_envelope_requires_every_contract_field() -> None:
    """All four fields are mandatory — none has a default to silently paper over."""
    for missing in ("event_id", "event_type", "correlation_id", "payload"):
        body = {
            "event_id": str(uuid4()),
            "event_type": "alert.normalized",
            "correlation_id": str(uuid4()),
            "payload": {},
        }
        del body[missing]
        with pytest.raises(ValidationError):
            EventEnvelope.model_validate(body)


def test_event_envelope_forbids_extra_fields() -> None:
    """An unknown field is a malformed delivery (422), never silently dropped.

    This is what stops the outbox row's private bookkeeping from being accepted
    as if it were part of the contract.
    """
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "event_id": str(uuid4()),
                "event_type": "alert.normalized",
                "correlation_id": str(uuid4()),
                "payload": {},
                "attempts": 3,
            }
        )


def test_event_envelope_payload_stays_open() -> None:
    """The envelope is generic transport: each agent judges its own payload shape."""
    envelope = EventEnvelope(
        event_id=uuid4(),
        event_type="alert.normalized",
        correlation_id=uuid4(),
        payload={"incident_id": str(uuid4()), "deduplicated": True, "nested": {"a": 1}},
    )
    assert envelope.payload["deduplicated"] is True
    assert envelope.payload["nested"] == {"a": 1}


def test_recommendation_created_payload_carries_ids_and_nothing_else() -> None:
    """The event names the recommendation; it does not copy it.

    A payload carrying root_cause/confidence/is_fallback would freeze them at the
    instant of writing — and a recommendation is the one row a human can CORRECT
    later, so the frozen copy could contradict the corrected row it names. The
    consumer reads the row by id and always sees current values.

    ``extra="forbid"`` makes that enforcement rather than convention: a producer that
    tries to helpfully attach the analysis is rejected, loudly, at the boundary.
    """
    incident_id, recommendation_id = uuid4(), uuid4()

    payload = RecommendationCreatedPayload(
        incident_id=incident_id, recommendation_id=recommendation_id
    )

    assert payload.incident_id == incident_id
    assert payload.recommendation_id == recommendation_id

    for stale in ("root_cause", "confidence", "is_fallback", "recommended_actions"):
        with pytest.raises(ValidationError):
            RecommendationCreatedPayload.model_validate(
                {
                    "incident_id": str(incident_id),
                    "recommendation_id": str(recommendation_id),
                    stale: "anything",
                }
            )
