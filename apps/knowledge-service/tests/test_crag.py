"""CRAG's decisions are pure, so they are proven here without a network.

The load-bearing test in this file is the one where everything grades
insufficient and the result is EMPTY. A grader that cannot return nothing is
ceremony — it costs a reason-mode call per incident and changes no decision — so
that case is what distinguishes this stage from an expensive no-op.
"""

from __future__ import annotations

import pytest
from radar_knowledge_service.crag import (
    CragParseError,
    Grade,
    apply_grades,
    build_grading_prompt,
    parse_grades,
)


def _c(chunk_id: str, **overrides: object) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "runbook_id": "rb",
        "section": "Summary",
        "text": f"text for {chunk_id}",
        **overrides,
    }


# ------------------------------------------------------- the negative result


def test_all_insufficient_returns_an_empty_context() -> None:
    """THE property this stage exists for.

    An incident whose service has runbooks but none that fit must produce NO
    context, so the reasoner is told the corpus does not cover this rather than
    grounding an RCA in the least-bad wrong answer. Every other test here passes
    for a grader that always says sufficient; this one does not.
    """
    chunks = [_c("a"), _c("b"), _c("c")]
    grades = dict.fromkeys(("a", "b", "c"), Grade.INSUFFICIENT)

    assert apply_grades(chunks, grades) == []


def test_one_usable_chunk_survives_a_mostly_insufficient_set() -> None:
    """Empty is for NOTHING usable, not for "mostly nothing"."""
    chunks = [_c("a"), _c("b"), _c("c")]
    grades = {
        "a": Grade.INSUFFICIENT,
        "b": Grade.PARTIAL,
        "c": Grade.INSUFFICIENT,
    }

    kept = apply_grades(chunks, grades)

    assert [c["chunk_id"] for c in kept] == ["b"]


# ----------------------------------------------------------------- filtering


def test_sufficient_and_partial_are_kept_and_insufficient_dropped() -> None:
    chunks = [_c("s"), _c("p"), _c("i")]
    grades = {
        "s": Grade.SUFFICIENT,
        "p": Grade.PARTIAL,
        "i": Grade.INSUFFICIENT,
    }

    kept = apply_grades(chunks, grades)

    assert [c["chunk_id"] for c in kept] == ["s", "p"]


def test_partial_is_kept_rather_than_dropped() -> None:
    """Pinned separately because it is a judgement call, not an obvious one.

    A related failure in the same service is genuine context for an engineer.
    Dropping partials would empty the context in exactly the ambiguous cases
    where some grounding beats none.
    """
    assert apply_grades([_c("p")], {"p": Grade.PARTIAL}) != []


def test_retrieval_order_is_preserved() -> None:
    """CRAG decides what is usable; it does not re-rank.

    Sorting by grade here would silently reintroduce a reordering stage after
    reranking was measured and removed.
    """
    chunks = [_c("first"), _c("second"), _c("third")]
    grades = {
        "first": Grade.PARTIAL,
        "second": Grade.SUFFICIENT,
        "third": Grade.PARTIAL,
    }

    kept = apply_grades(chunks, grades)

    assert [c["chunk_id"] for c in kept] == ["first", "second", "third"]


def test_each_kept_chunk_carries_its_grade() -> None:
    """The reasoner is told HOW well each chunk fits, not just that it passed."""
    (kept,) = apply_grades([_c("a")], {"a": Grade.PARTIAL})

    assert kept["grade"] == "partial"
    assert kept["text"] == "text for a"


def test_grading_nothing_returns_nothing() -> None:
    assert apply_grades([], {}) == []


# ------------------------------------------------------------------- parsing


def test_a_well_formed_reply_parses() -> None:
    payload = (
        '{"grades": [{"chunk_id": "a", "grade": "sufficient"}, '
        '{"chunk_id": "b", "grade": "insufficient"}]}'
    )

    assert parse_grades(payload, ["a", "b"]) == {
        "a": Grade.SUFFICIENT,
        "b": Grade.INSUFFICIENT,
    }


def test_a_fenced_reply_still_parses() -> None:
    payload = '```json\n{"grades": [{"chunk_id": "a", "grade": "partial"}]}\n```'

    assert parse_grades(payload, ["a"]) == {"a": Grade.PARTIAL}


def test_an_incomplete_reply_is_refused() -> None:
    """Three grades for five chunks is not a partial success.

    The two ungraded chunks would have to be guessed at, and guessing either way
    is wrong: insufficient lets a truncated reply silently empty the context,
    sufficient lets an ungraded chunk reach the reasoner wearing a grade it was
    never given.
    """
    payload = '{"grades": [{"chunk_id": "a", "grade": "sufficient"}]}'

    with pytest.raises(CragParseError, match="ungraded"):
        parse_grades(payload, ["a", "b"])


def test_a_grade_for_a_chunk_that_was_never_sent_is_refused() -> None:
    """The model is answering about something other than the excerpts given."""
    payload = (
        '{"grades": [{"chunk_id": "a", "grade": "sufficient"}, '
        '{"chunk_id": "ghost", "grade": "sufficient"}]}'
    )

    with pytest.raises(CragParseError, match="never sent"):
        parse_grades(payload, ["a"])


def test_a_duplicate_grade_is_refused() -> None:
    payload = (
        '{"grades": [{"chunk_id": "a", "grade": "sufficient"}, '
        '{"chunk_id": "a", "grade": "insufficient"}]}'
    )

    with pytest.raises(CragParseError, match="more than once"):
        parse_grades(payload, ["a"])


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        '{"grades": [{"chunk_id": "a"}]}',
        '{"grades": [{"chunk_id": "a", "grade": "maybe"}]}',
        '{"grades": [{"chunk_id": "a", "grade": 7}]}',
        '{"grades": [{"chunk_id": "a", "grade": "sufficient", "why": "x"}]}',
    ],
)
def test_malformed_replies_raise(payload: str) -> None:
    """An invented grade like "maybe" must not be coerced into a real one."""
    with pytest.raises(CragParseError):
        parse_grades(payload, ["a"])


# -------------------------------------------------------------------- prompt


def test_the_prompt_carries_the_incident_and_every_chunk() -> None:
    prompt = build_grading_prompt("orders failing", [_c("a"), _c("b")])

    assert "orders failing" in prompt
    assert "chunk_id: a" in prompt
    assert "chunk_id: b" in prompt
    assert "text for a" in prompt


def test_the_prompt_refuses_an_empty_chunk_list() -> None:
    """Spending a reason-mode call to grade nothing."""
    with pytest.raises(ValueError, match="no chunks"):
        build_grading_prompt("q", [])
