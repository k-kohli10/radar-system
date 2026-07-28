"""Callback parsing — the deep-treatment, mutation-proven (pure, no I/O).

A mis-parse writes feedback against the wrong recommendation or resolves the wrong
incident, so this is where the rigor goes. Three properties are load-bearing, each
mutation-proven:

- **The action mapping is exact.** Each action_id maps to its own action, and only
  it. Mutation: hardcode one action / ignore action_id -> the other actions'
  assertions go red.
- **An unknown action is REJECTED, never defaulted.** Mutation: default an unknown
  action_id instead of raising -> the unknown-action test goes red. Defaulting an
  un-understood click would act on an intent the user never expressed.
- **A missing or malformed value is REJECTED, never passed through.** Mutation: drop
  the UUID validation -> a garbage value reaches a query as-is, and the malformed-
  value test goes red.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from radar_contracts import NotificationInteraction
from radar_feedback_service.callbacks import (
    CallbackParseError,
    InteractionAction,
    parse_callback,
)


def _interaction(*, action_id: str, value: str | None) -> NotificationInteraction:
    return NotificationInteraction(
        action_id=action_id,
        value=value,
        user_id="U1",
        channel_id="C1",
        message_ts="1720000000.0001",
    )


# --- the action mapping is exact -------------------------------------------------


@pytest.mark.parametrize(
    ("action_id", "expected"),
    [
        ("feedback.up", InteractionAction.FEEDBACK_UP),
        ("feedback.down", InteractionAction.FEEDBACK_DOWN),
        ("incident.resolve", InteractionAction.RESOLVE),
    ],
)
def test_each_action_id_maps_to_its_action(
    action_id: str, expected: InteractionAction
) -> None:
    rec_id = uuid4()
    parsed = parse_callback(_interaction(action_id=action_id, value=str(rec_id)))
    assert parsed.action is expected
    assert parsed.recommendation_id == rec_id


def test_action_id_set_is_exactly_the_three_v1_actions() -> None:
    """Guard the closed vocabulary itself: correction is NOT here (deferred — see
    "Correction-gated re-reason" in docs/roadmap.md)."""
    assert {a.value for a in InteractionAction} == {
        "feedback.up",
        "feedback.down",
        "incident.resolve",
    }


# --- routing fields carried through ----------------------------------------------


def test_routing_fields_pass_through() -> None:
    parsed = parse_callback(_interaction(action_id="feedback.up", value=str(uuid4())))
    assert parsed.user_id == "U1"
    assert parsed.channel_id == "C1"
    assert parsed.message_ts == "1720000000.0001"


# --- rejects: never guess --------------------------------------------------------


def test_unknown_action_id_is_rejected_not_defaulted() -> None:
    """An action_id outside the closed set raises — the parser never picks a default.

    This is the load-bearing 'never guess' guard: a card from a future version, or a
    crafted payload, must do NOTHING rather than be mapped to (say) a resolve.
    """
    with pytest.raises(CallbackParseError, match="unknown interaction action_id"):
        parse_callback(_interaction(action_id="feedback.sideways", value=str(uuid4())))


def test_missing_value_is_rejected() -> None:
    with pytest.raises(CallbackParseError, match="no value"):
        parse_callback(_interaction(action_id="feedback.up", value=None))


@pytest.mark.parametrize(
    "bad_value",
    ["not-a-uuid", "", "12345", "feedback.up", "'; DROP TABLE recommendations; --"],
)
def test_malformed_value_is_rejected_not_passed_through(bad_value: str) -> None:
    """A value that is not a UUID raises rather than reaching a query as a raw
    string — the guard against acting on a mis-identified recommendation."""
    with pytest.raises(CallbackParseError, match="not a recommendation id"):
        parse_callback(_interaction(action_id="incident.resolve", value=bad_value))


def test_valid_uuid_value_is_parsed_to_a_uuid() -> None:
    rec_id = uuid4()
    parsed = parse_callback(
        _interaction(action_id="incident.resolve", value=str(rec_id))
    )
    assert isinstance(parsed.recommendation_id, UUID)
    assert parsed.recommendation_id == rec_id
