"""The adversarial corpus: everything a language model might actually send back.

The input space here is hostile in a way nothing else in this codebase is. Every other
parser in RADAR reads something a RADAR component produced, under a contract both sides
share. This one reads whatever a model felt like emitting, and the prompt asking for
"valid JSON, no text before or after" is a request, not a guarantee.

THE FAILURE THIS FILE IS BUILT AROUND: THE HALF-SUCCESS

The dangerous response is not the one that fails cleanly. It is the one that parses far
enough to populate ``root_cause`` and then falls over on ``recommended_actions``. A
parser that walked the fields one at a time would keep the half it liked and store a
confident root-cause paragraph with an EMPTY action list — a card that tells an engineer
what broke and then tells them to do nothing.

That is worse than the template fallback, which at least hands over the investigation
checklist. And it is worse *quietly*: the row looks fine, ``is_fallback`` is false, and
nobody finds out.

So the tests below assert, for every malformed variant, that the result is a FAILURE and
NOT a ParsedRCA — including the ones whose ``root_cause`` is perfectly good. The
mutation that proves it: relax the actions validation, and
``test_a_good_root_cause_with_broken_actions_fails_entirely`` goes red, because the
parser starts returning exactly the half-built RCA this whole design exists to prevent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from radar_contracts import Confidence
from radar_reasoner_agent.rca import (
    ParsedRCA,
    RCAParseFailure,
    RCAParseFailureReason,
    parse_rca,
)

GOOD_ROOT_CAUSE = (
    "The order-service deployment at 10:14 introduced a connection leak; the "
    "order-db pool saturated three minutes later."
)

VALID: dict[str, Any] = {
    "root_cause": GOOD_ROOT_CAUSE,
    "confidence": "high",
    "recommended_actions": [
        {"order": 1, "action": "Roll back order-service to the previous revision"},
        {"order": 2, "action": "Check order-db active connection count in Grafana"},
    ],
}


def _json(document: Any) -> str:
    return json.dumps(document)


# ==========================================================================
# THE HALF-SUCCESS — the whole point of this module
# ==========================================================================


@pytest.mark.parametrize(
    ("broken_actions", "what"),
    [
        pytest.param(None, "actions-missing", id="actions-missing"),
        pytest.param([], "actions-empty", id="actions-empty"),
        pytest.param([{"order": 1}], "action-has-no-text", id="action-missing-text"),
        pytest.param(
            [{"action": "do a thing"}], "action-has-no-order", id="action-missing-order"
        ),
        pytest.param(
            [{"order": 0, "action": "do a thing"}], "order-zero", id="order-zero"
        ),
        pytest.param(
            [{"order": -1, "action": "do a thing"}], "order-negative", id="order-neg"
        ),
        pytest.param(
            [{"order": 1, "action": ""}], "action-text-empty", id="action-text-empty"
        ),
        pytest.param("roll it back", "actions-not-a-list", id="actions-not-a-list"),
        pytest.param(
            [{"order": 1, "action": "x"}, "and another"],
            "one-action-is-garbage",
            id="one-action-garbage",
        ),
    ],
)
def test_a_good_root_cause_with_broken_actions_fails_entirely(
    broken_actions: Any, what: str
) -> None:
    """THE test. The root_cause is perfect. The actions are not. NOTHING is returned.

    A naive parser stores the half it liked: a confident paragraph explaining the
    outage, and an empty list of things to do about it. The engineer reads a card
    that says what broke and then recommends nothing — worse than the template
    fallback, which at least hands them the investigation checklist, and worse
    *quietly*, because is_fallback is false and the row looks fine.

    Every variant here has a flawless root_cause. Every one must fail completely.
    """
    document = {**VALID}
    if broken_actions is None:
        document.pop("recommended_actions")
    else:
        document["recommended_actions"] = broken_actions

    result = parse_rca(_json(document))

    assert isinstance(result, RCAParseFailure), (
        f"{what}: the parser returned an RCA. If root_cause survived and the actions "
        "did not, it has stored a half-built recommendation — a card that explains the "
        "outage and tells the engineer to do nothing."
    )
    assert result.reason is RCAParseFailureReason.SCHEMA_INVALID
    # And the good half is nowhere: there is no object to have leaked it into.
    assert not isinstance(result, ParsedRCA)


def test_broken_root_cause_with_good_actions_also_fails_entirely() -> None:
    """The mirror: the actions are fine and the root_cause is missing. Still nothing.

    All-or-nothing is symmetric. A recommendation with actions but no explanation is
    just as unusable as the reverse.
    """
    document = {**VALID}
    document.pop("root_cause")

    result = parse_rca(_json(document))

    assert isinstance(result, RCAParseFailure)
    assert result.reason is RCAParseFailureReason.SCHEMA_INVALID


# ==========================================================================
# WHAT WE ACCEPT — liberal about form, strict about content
# ==========================================================================


def test_the_clean_case() -> None:
    result = parse_rca(_json(VALID))

    assert isinstance(result, ParsedRCA)
    assert result.root_cause == GOOD_ROOT_CAUSE
    assert result.confidence is Confidence.HIGH
    assert [a.order for a in result.recommended_actions] == [1, 2]
    assert "Roll back order-service" in result.recommended_actions[0].action


@pytest.mark.parametrize(
    ("wrapper", "what"),
    [
        pytest.param("```json\n{body}\n```", "fenced-with-language", id="fence-json"),
        pytest.param("```\n{body}\n```", "fenced-bare", id="fence-bare"),
        pytest.param(
            "Here is my analysis:\n\n{body}", "prose-before", id="prose-before"
        ),
        pytest.param(
            "{body}\n\nLet me know if you need more detail.",
            "prose-after",
            id="prose-after",
        ),
        pytest.param(
            "Sure! Here you go:\n```json\n{body}\n```\nHope that helps.",
            "prose-and-fence",
            id="prose-and-fence",
        ),
        pytest.param("   \n\n{body}\n\n  ", "whitespace", id="whitespace"),
    ],
)
def test_the_json_is_extracted_from_whatever_the_model_wrapped_it_in(
    wrapper: str, what: str
) -> None:
    """Models fence and chatter no matter what the prompt says. Extract, then validate.

    This cannot smuggle anything past validation: whatever comes out of the extractor is
    validated in full, so a bad extraction produces the same failure we would have had.
    The worst case is a fallback; the best case is a real RCA we would otherwise have
    thrown away over punctuation.
    """
    raw = wrapper.replace("{body}", _json(VALID))

    result = parse_rca(raw)

    assert isinstance(result, ParsedRCA), f"{what}: the RCA was thrown away"
    assert result.root_cause == GOOD_ROOT_CAUSE


@pytest.mark.parametrize(
    "confidence",
    ["high", "High", "HIGH", "  high  "],
    ids=["lower", "title", "upper", "padded"],
)
def test_confidence_case_is_normalized(confidence: str) -> None:
    """ "High" means high. Falling back over a capital letter would be absurd."""
    result = parse_rca(_json({**VALID, "confidence": confidence}))

    assert isinstance(result, ParsedRCA)
    assert result.confidence is Confidence.HIGH


def test_an_extra_key_is_ignored_not_fatal() -> None:
    """A chatty model has still answered the question.

    A DELIBERATE departure from the repo's extra="forbid" discipline. That rule
    exists for data WE produce, where an unexpected key means one of our own
    components drifted. This is a language model: an extra key means it was verbose,
    not that anything is broken, and discarding a good analysis over it would trade a
    real RCA for a template.
    """
    result = parse_rca(
        _json({**VALID, "reasoning": "I looked at the deployment timeline first."})
    )

    assert isinstance(result, ParsedRCA)
    assert result.root_cause == GOOD_ROOT_CAUSE


def test_extra_actions_beyond_two_are_fine() -> None:
    document = {
        **VALID,
        "recommended_actions": [
            {"order": i, "action": f"step {i}"} for i in range(1, 8)
        ],
    }

    result = parse_rca(_json(document))

    assert isinstance(result, ParsedRCA)
    assert len(result.recommended_actions) == 7


# ==========================================================================
# WHAT WE REFUSE
# ==========================================================================


@pytest.mark.parametrize(
    ("raw", "what"),
    [
        pytest.param("", "empty-string", id="empty"),
        pytest.param("   \n  ", "whitespace-only", id="whitespace-only"),
        pytest.param(
            "I'm sorry, I don't have enough information to determine a root cause.",
            "an-apology",
            id="apology",
        ),
        pytest.param("The root cause is a bad deploy.", "prose", id="prose"),
        pytest.param('{"root_cause": "x", ', "truncated-json", id="truncated"),
        pytest.param("[1, 2, 3]", "a-json-array", id="array"),
        pytest.param('"just a string"', "a-json-string", id="bare-string"),
        pytest.param("null", "json-null", id="null"),
        pytest.param("42", "a-number", id="number"),
        pytest.param(
            "```json\nnot json inside\n```", "fenced-garbage", id="fenced-bad"
        ),
    ],
)
def test_anything_that_is_not_a_json_object_is_not_json(raw: str, what: str) -> None:
    """No object, no RCA. The fallback covers every one of these."""
    result = parse_rca(raw)

    assert isinstance(result, RCAParseFailure), f"{what} was accepted as an RCA"
    assert result.reason is RCAParseFailureReason.NOT_JSON


@pytest.mark.parametrize(
    ("confidence", "what"),
    [
        pytest.param("very high", "not-in-the-vocabulary", id="very-high"),
        pytest.param("certain", "invented", id="certain"),
        pytest.param("", "empty", id="empty"),
        pytest.param(0.9, "a-number", id="number"),
        pytest.param(None, "null", id="null"),
    ],
)
def test_a_confidence_outside_the_vocabulary_is_refused(
    confidence: Any, what: str
) -> None:
    """Guessing which of low/medium/high "very high" meant would be inventing data.

    Confidence is a closed vocabulary because downstream compares it — a Slack card
    colours on it, and Phase 9's feedback groups by it. A value we cannot compare is
    worse than no value.
    """
    result = parse_rca(_json({**VALID, "confidence": confidence}))

    assert isinstance(result, RCAParseFailure), f"{what} was accepted"
    assert result.reason is RCAParseFailureReason.SCHEMA_INVALID


@pytest.mark.parametrize(
    ("root_cause", "what"),
    [
        pytest.param("", "empty-string", id="empty"),
        pytest.param(None, "null", id="null"),
        pytest.param(42, "a-number", id="number"),
        pytest.param({"why": "because"}, "an-object", id="object"),
    ],
)
def test_a_missing_or_unusable_root_cause_is_refused(
    root_cause: Any, what: str
) -> None:
    result = parse_rca(_json({**VALID, "root_cause": root_cause}))

    assert isinstance(result, RCAParseFailure), f"{what} was accepted"
    assert result.reason is RCAParseFailureReason.SCHEMA_INVALID


def test_the_empty_action_list_decision_is_a_failure_not_an_empty_success() -> None:
    """Stated explicitly, because it is a real decision that could have gone the
    other way.

    An RCA with no actions is useless to an engineer at 3am. The template fallback is
    strictly BETTER: it carries the plan's real investigation steps. So zero actions
    is a failure, we fall back, and the engineer gets a checklist instead of an empty
    card. Accepting it would have traded a usable fallback for an unusable success.
    """
    result = parse_rca(_json({**VALID, "recommended_actions": []}))

    assert isinstance(result, RCAParseFailure)
    assert result.reason is RCAParseFailureReason.SCHEMA_INVALID
