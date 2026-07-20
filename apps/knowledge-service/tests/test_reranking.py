"""Reranking's decisions are pure, so they are proven here without a network.

The stage is judged against pre-registered rank targets, so its ordering rule has
to be pinned precisely: a rank that moved because of an unstated tiebreak would
be credited to reranking while actually being an artifact.
"""

from __future__ import annotations

from typing import Any

import pytest
from radar_knowledge_service.reranking import (
    RerankParseError,
    build_rerank_prompt,
    parse_rerank_scores,
    rerank_by_scores,
)


def _c(chunk_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "runbook_id": "rb",
        "section": "Summary",
        "text": f"text for {chunk_id}",
        **overrides,
    }


# ------------------------------------------------------------------ ordering


def test_candidates_are_reordered_by_score() -> None:
    ordered = rerank_by_scores([_c("a"), _c("b"), _c("c")], {"a": 1, "b": 9, "c": 5})

    assert [c["chunk_id"] for c in ordered] == ["b", "c", "a"]


def test_a_tie_leaves_the_incoming_order_alone() -> None:
    """THE property the evaluation depends on.

    An LLM asked for integer scores returns ties constantly. If ties resolved by
    anything other than a stated rule, a rank could move because Elasticsearch
    returned hits in a different order, and the move would be credited to
    reranking. Here every score is equal, so the fused order must survive intact.

    NOTE ON WHAT THIS CAN AND CANNOT PROVE. Deleting the position and id terms
    from the sort key does NOT fail this test, or any test, because it does not
    change behaviour: position is the arrival index and Python's sort is stable,
    so the explicit rule and the implicit one coincide. That mutant is
    equivalent, not a coverage gap — verified by differential testing over 20k
    tie-heavy inputs. The terms stay as documentation and as protection if this
    ever sorts something other than a list of unique ids, which is exactly the
    case where the fusion core's identical-looking omission IS a real bug.
    """
    candidates = [_c("z"), _c("m"), _c("a")]

    ordered = rerank_by_scores(candidates, {"z": 5, "m": 5, "a": 5})

    assert [c["chunk_id"] for c in ordered] == ["z", "m", "a"]


def test_ties_do_not_fall_back_on_chunk_id_before_position() -> None:
    """Position outranks id in the tiebreak, and the order matters.

    If id came first, a tie would REORDER the fused result alphabetically —
    discarding the pipeline's existing belief for no reason. This fails against
    that version because alphabetical order here differs from incoming order.
    """
    ordered = rerank_by_scores([_c("z"), _c("a")], {"z": 7, "a": 7})

    assert [c["chunk_id"] for c in ordered] == ["z", "a"]


def test_the_result_is_deterministic_across_repeated_calls() -> None:
    candidates = [_c("b"), _c("a"), _c("c")]
    scores: dict[str, float] = {"a": 3, "b": 3, "c": 3}
    first = rerank_by_scores(candidates, scores)

    assert all(rerank_by_scores(candidates, scores) == first for _ in range(20))


def test_a_promoted_candidate_can_come_from_the_back() -> None:
    """The point of the stage: rank 4 by fusion can become rank 1."""
    candidates = [_c("a"), _c("b"), _c("c"), _c("d")]

    ordered = rerank_by_scores(candidates, {"a": 2, "b": 2, "c": 2, "d": 10})

    assert ordered[0]["chunk_id"] == "d"


# ------------------------------------------------- unscored and bogus entries


def test_an_unscored_candidate_is_kept_after_the_scored_ones() -> None:
    """A truncated reply must not silently shorten retrieval."""
    ordered = rerank_by_scores([_c("a"), _c("b"), _c("c")], {"b": 9})

    assert [c["chunk_id"] for c in ordered] == ["b", "a", "c"]


def test_unscored_candidates_keep_their_incoming_order() -> None:
    ordered = rerank_by_scores([_c("x"), _c("y"), _c("z")], {})

    assert [c["chunk_id"] for c in ordered] == ["x", "y", "z"]


def test_a_score_for_an_unknown_id_cannot_inject_a_document() -> None:
    """A hallucinated id must not become a retrieval result."""
    ordered = rerank_by_scores([_c("a")], {"a": 1, "invented": 10})

    assert [c["chunk_id"] for c in ordered] == ["a"]


def test_a_zero_score_is_a_score_not_an_absence() -> None:
    """0 means "unrelated" and must outrank an UNSCORED chunk's fallback.

    Treating 0 as missing would let a chunk the model explicitly rejected sort
    with the ones it never saw — losing the only signal the model gave.
    """
    ordered = rerank_by_scores([_c("unscored"), _c("zero")], {"zero": 0})

    assert [c["chunk_id"] for c in ordered] == ["zero", "unscored"]


def test_limit_truncates_after_reordering() -> None:
    ordered = rerank_by_scores([_c("a"), _c("b"), _c("c")], {"c": 9}, limit=1)

    assert [c["chunk_id"] for c in ordered] == ["c"]


def test_reranking_nothing_returns_nothing() -> None:
    assert rerank_by_scores([], {"a": 1}) == []


# -------------------------------------------------------------------- parsing


def test_a_well_formed_reply_parses_to_scores() -> None:
    payload = (
        '{"scores": [{"chunk_id": "a", "score": 9}, {"chunk_id": "b", "score": 2}]}'
    )

    assert parse_rerank_scores(payload) == {"a": 9.0, "b": 2.0}


def test_a_fenced_reply_still_parses() -> None:
    """Wrapping JSON in a code fence is a formatting habit, not a failure.

    Rejecting it would discard a usable answer and degrade retrieval for a
    cosmetic reason.
    """
    payload = '```json\n{"scores": [{"chunk_id": "a", "score": 7}]}\n```'

    assert parse_rerank_scores(payload) == {"a": 7.0}


def test_prose_around_the_json_is_refused() -> None:
    """Not tolerated, because guessing where the JSON starts invites
    misparsing a model that changed its mind mid-reply."""
    with pytest.raises(RerankParseError):
        parse_rerank_scores('Here are the scores: {"scores": []}')


def test_a_duplicate_chunk_id_is_refused() -> None:
    """First-wins and last-wins are both defensible, which is the reason not to
    pick one silently."""
    payload = (
        '{"scores": [{"chunk_id": "a", "score": 9}, {"chunk_id": "a", "score": 1}]}'
    )

    with pytest.raises(RerankParseError, match="more than once"):
        parse_rerank_scores(payload)


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "{}",
        '{"scores": [{"chunk_id": "a"}]}',
        '{"scores": [{"score": 5}]}',
        '{"scores": [{"chunk_id": "a", "score": 99}]}',
        '{"scores": [{"chunk_id": "a", "score": -1}]}',
        '{"scores": [{"chunk_id": "a", "score": 5, "extra": 1}]}',
    ],
)
def test_malformed_replies_raise_rather_than_returning_partial_scores(
    payload: str,
) -> None:
    """Out-of-range scores are refused too.

    A 99 would dominate every real score and silently make one chunk
    unbeatable — worse than no reranking, and invisible without this check.
    """
    with pytest.raises(RerankParseError):
        parse_rerank_scores(payload)


# --------------------------------------------------------------------- prompt


def test_the_prompt_carries_ids_text_and_the_query() -> None:
    """Ids are how scores join back to candidates; text is what is judged."""
    prompt = build_rerank_prompt("orders failing", [_c("a"), _c("b")])

    assert "orders failing" in prompt
    assert "chunk_id: a" in prompt
    assert "chunk_id: b" in prompt
    assert "text for a" in prompt


def test_the_prompt_refuses_an_empty_candidate_list() -> None:
    """Asking a model to rank nothing spends a reason-mode call on no question."""
    with pytest.raises(ValueError, match="no candidates"):
        build_rerank_prompt("q", [])
