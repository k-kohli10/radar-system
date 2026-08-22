"""RCA card formatting — a pure function, ordinary tests.

These pin the layout the on-call engineer reads: the header, the summary fields,
the root cause, the numbered actions in order, and the AI-Unavailable variant.
No database, no Slack — the formatter takes plain values and returns blocks.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import uuid4

from radar_contracts import NotificationInteraction, RecommendedAction
from radar_feedback_service.callbacks import parse_callback
from radar_feedback_service.cards import RcaCardData, format_rca_card


def _data(**over: Any) -> RcaCardData:
    fields: dict[str, Any] = {
        "incident_id": uuid4(),
        "recommendation_id": uuid4(),
        "service_name": "order-service",
        "title": "order-service OrderProcessingFailureRate",
        "severity": "high",
        "status": "investigating",
        "root_cause": "A bad deploy raised the DB connection pool timeout.",
        "confidence": "high",
        "recommended_actions": [
            RecommendedAction(order=1, action="Roll back the last deploy."),
            RecommendedAction(order=2, action="Verify pool metrics recover."),
        ],
        "is_fallback": False,
    }
    fields.update(over)
    return RcaCardData(**fields)


def _texts(blocks: list[dict[str, object]]) -> str:
    """All rendered text in the card, flattened — for substring assertions.

    ``ensure_ascii=False``: the card renders emoji literally (Slack's mrkdwn takes
    real unicode, not escapes), so a test checking for e.g. "🟢 Low" must see the
    same literal character, not json.dumps's default ``\\ud83d\\udfe2`` escape.
    """
    return json.dumps(blocks, ensure_ascii=False)


def test_header_and_fallback_text_for_ai_analysis() -> None:
    fallback_text, blocks = format_rca_card(_data())
    header = blocks[0]
    assert header["type"] == "header"
    assert "RADAR Alert" in header["text"]["text"]  # type: ignore[index]
    assert "AI Unavailable" not in header["text"]["text"]  # type: ignore[index]
    # The notification/preview string is meaningful, not blank.
    assert "order-service OrderProcessingFailureRate" in fallback_text


def test_header_leads_with_alert_name_not_the_service() -> None:
    # The title is "<service> <alert_name>"; the header shows just the alert (the
    # service has its own field), so a channel of cards is scannable by alert name.
    _, blocks = format_rca_card(
        _data(
            service_name="order-service",
            title="order-service OrderServiceHighMemory",
        )
    )
    header = blocks[0]["text"]["text"]  # type: ignore[index]
    assert "OrderServiceHighMemory" in header
    assert "order-service" not in header  # the service prefix is stripped, not doubled


def test_summary_fields_present() -> None:
    _, blocks = format_rca_card(_data())
    text = _texts(blocks)
    assert "order-service" in text
    assert "🟠 High" in text  # severity: icon + capitalized
    assert "Investigating" in text  # status capitalized
    assert "🟢 High" in text  # confidence: icon + capitalized


def test_severity_and_confidence_get_distinct_icon_scales() -> None:
    """Severity and confidence read opposite directions — high severity is a red
    flag, high confidence is a green one — so they must not share one icon map."""
    _, blocks = format_rca_card(_data(severity="low", confidence="low"))
    text = _texts(blocks)
    assert "🟢 Low" in text  # severity: low is calm, green
    assert "🔴 Low" in text  # confidence: low is a warning, red


def test_unrecognized_severity_falls_back_to_a_visible_placeholder() -> None:
    """An icon map miss must not crash the card — same 'visible slip, not silent
    failure' rule as the empty-actions placeholder."""
    _, blocks = format_rca_card(_data(severity="unknown-value"))
    text = _texts(blocks)
    assert "⚪ Unknown-value" in text


def test_root_cause_and_actions_rendered_in_order() -> None:
    _, blocks = format_rca_card(_data())
    text = _texts(blocks)
    assert "A bad deploy raised the DB connection pool timeout." in text
    # Actions are numbered in order.
    assert "1. Roll back the last deploy." in text
    assert "2. Verify pool metrics recover." in text


def test_actions_are_sorted_by_order_not_input_order() -> None:
    """Actions render in ``order``, however the list arrived."""
    _, blocks = format_rca_card(
        _data(
            recommended_actions=[
                RecommendedAction(order=2, action="second"),
                RecommendedAction(order=1, action="first"),
            ]
        )
    )
    text = _texts(blocks)
    assert text.index("1. first") < text.index("2. second")


def test_incident_id_in_context() -> None:
    data = _data()
    _, blocks = format_rca_card(data)
    assert str(data.incident_id) in _texts(blocks)


def test_action_buttons_carry_the_parser_contract() -> None:
    """The three buttons the click path reads back: each action_id is a value from the
    closed InteractionAction set, and each value is the recommendation id. This is the
    send half of the send/read contract — if it drifts, every click mis-parses."""
    data = _data()
    _, blocks = format_rca_card(data)
    actions = next(b for b in blocks if b["type"] == "actions")
    elements = cast("list[dict[str, Any]]", actions["elements"])
    by_action = {e["action_id"]: e for e in elements}
    assert set(by_action) == {"feedback.up", "feedback.down", "incident.resolve"}
    assert all(e["value"] == str(data.recommendation_id) for e in elements)
    # Round-trips through the actual parser: a click on each button parses back to the
    # same recommendation id and the matching action — the two halves cannot disagree.
    for action_id, element in by_action.items():
        parsed = parse_callback(
            NotificationInteraction(
                action_id=action_id,
                value=element["value"],
                user_id="U1",
                channel_id="C1",
                message_ts="1720000000.0001",
            )
        )
        assert parsed.action.value == action_id
        assert parsed.recommendation_id == data.recommendation_id


def test_resolve_button_is_styled_primary_not_danger() -> None:
    """Resolve is the forward-moving, affirmative action — Slack's green, not red."""
    _, blocks = format_rca_card(_data(status="investigating"))
    actions = next(b for b in blocks if b["type"] == "actions")
    elements = cast("list[dict[str, Any]]", actions["elements"])
    resolve = next(e for e in elements if e["action_id"] == "incident.resolve")
    assert resolve["style"] == "primary"


def test_resolved_incident_hides_the_resolve_button() -> None:
    """Slack buttons have no disabled state — once resolved, Resolve is gone, not
    greyed out.

    Mutation check: if ``_actions_block`` stopped reading ``resolved`` (e.g. the branch
    were deleted), a resolved card would still carry all three buttons and this would
    fail — proving the omission is load-bearing, not incidental.
    """
    _, blocks = format_rca_card(_data(status="resolved"))
    actions = next(b for b in blocks if b["type"] == "actions")
    elements = cast("list[dict[str, Any]]", actions["elements"])
    action_ids = {e["action_id"] for e in elements}
    assert action_ids == {"feedback.up", "feedback.down"}
    assert "incident.resolve" not in action_ids


def test_resolved_incident_shows_a_static_resolved_note() -> None:
    """The static line replaces the missing button as the visual confirmation."""
    _, blocks = format_rca_card(_data(status="resolved"))
    assert "Resolved" in _texts(blocks)
    assert "no further action needed" in _texts(blocks)


def test_open_incident_still_shows_all_three_buttons() -> None:
    """A non-resolved status (open/investigating) is unaffected by the resolved gate."""
    _, blocks = format_rca_card(_data(status="investigating"))
    actions = next(b for b in blocks if b["type"] == "actions")
    elements = cast("list[dict[str, Any]]", actions["elements"])
    assert {e["action_id"] for e in elements} == {
        "feedback.up",
        "feedback.down",
        "incident.resolve",
    }


def test_fallback_variant_marks_ai_unavailable() -> None:
    """The AI-Unavailable card must be unmistakable: header and a footer note.

    An engineer must not read the planner's template checklist as a model's
    diagnosis, so both ends of the card say the AI was unavailable.
    """
    fallback_text, blocks = format_rca_card(_data(is_fallback=True, confidence="low"))
    assert "AI Unavailable" in blocks[0]["text"]["text"]  # type: ignore[index]
    assert "AI Unavailable" in fallback_text
    text = _texts(blocks)
    assert "without AI" in text
    assert "Low" in text


def test_empty_actions_render_a_visible_placeholder() -> None:
    """A contract slip (no actions) shows on the card, not a blank section."""
    _, blocks = format_rca_card(_data(recommended_actions=[]))
    assert "No actions recorded" in _texts(blocks)


def test_oversized_root_cause_is_truncated_to_fit_slack() -> None:
    """A root cause over Slack's 3000-char block limit is cut, not left to fail
    the whole post — the incident must still reach the channel."""
    _, blocks = format_rca_card(_data(root_cause="x" * 5000))
    root_section = blocks[3]
    rendered = root_section["text"]["text"]  # type: ignore[index]
    assert len(rendered) <= 2900
    assert rendered.endswith("… (truncated)")
