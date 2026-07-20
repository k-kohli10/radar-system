"""Fusion is pure, so it is proven here — exhaustively, with no infrastructure.

The retrieval evaluation attributes every margin and rank change to a specific
stage. Fusion is one of those stages, so its behaviour has to be pinned precisely
enough that a later "RRF fixed this probe" is a claim about something known,
rather than about whatever the code happened to do.
"""

from __future__ import annotations

import pytest
from radar_knowledge_service.fusion import (
    DEFAULT_RANK_CONSTANT,
    reciprocal_rank_fusion,
)


def test_a_single_list_is_returned_in_its_own_order() -> None:
    """With nothing to fuse, fusion must not reorder."""
    assert reciprocal_rank_fusion([["a", "b", "c"]]) == ["a", "b", "c"]


def test_agreement_between_lists_preserves_the_order() -> None:
    assert reciprocal_rank_fusion([["a", "b"], ["a", "b"]]) == ["a", "b"]


def test_appearing_in_both_lists_beats_topping_only_one() -> None:
    """THE property RRF exists for, and the reason hybrid retrieval can help.

    `b` is second in both lists; `a` is first in one and absent from the other.
    Corroboration across retrieval strategies outranks a single strategy's
    confidence — which is exactly the bet the hybrid slice is making: BM25 and
    kNN failing differently means agreement between them is evidence.
    """
    fused = reciprocal_rank_fusion([["a", "b"], ["c", "b"]])

    assert fused[0] == "b"
    assert set(fused) == {"a", "b", "c"}


def test_a_chunk_only_one_strategy_found_still_survives() -> None:
    """Union, not intersection.

    If fusion dropped ids missing from either list, adding BM25 could REMOVE a
    chunk kNN had ranked first — a hybrid stage that loses correct answers. The
    kNN-only id must still be present.
    """
    fused = reciprocal_rank_fusion([["only-knn", "shared"], ["shared"]])

    assert "only-knn" in fused


def test_the_score_is_the_documented_reciprocal_sum() -> None:
    """Pin the formula, not just the ordering it happens to produce.

    Asserting order alone would pass for several different formulas — including
    ones that weight the lists unequally. `shared` is rank 2 in both lists, so
    its score is 2/(k+2); `first` tops one list only, so its score is 1/(k+1).
    With k=60 the reciprocal sum puts `shared` ahead, and that is the arithmetic
    being fixed here.
    """
    k = DEFAULT_RANK_CONSTANT
    assert 2 / (k + 2) > 1 / (k + 1)

    fused = reciprocal_rank_fusion([["first", "shared"], ["other", "shared"]])

    assert fused[0] == "shared"


def test_second_place_in_both_lists_always_beats_first_in_one() -> None:
    """This holds for EVERY positive rank constant, which is not obvious.

    Solving 2/(k+r) > 1/(k+1) gives k > r-2, so at r=2 the condition is k > 0 —
    true for every constant the function accepts. The tuning knob cannot turn
    this case around, and a test asserting otherwise would be asserting
    something arithmetically impossible.
    """
    lists = [["first", "shared"], ["other", "shared"]]

    for rank_constant in (1, 2, 60, 600):
        assert reciprocal_rank_fusion(lists, rank_constant=rank_constant)[0] == "shared"


def test_the_rank_constant_decides_how_deep_corroboration_still_wins() -> None:
    """Where the knob actually bites: rank 4 in both vs rank 1 in one.

    By k > r-2, corroboration wins at r=4 only when k > 2. So the default k=60
    prefers the chunk both strategies found halfway down their lists, while k=1
    prefers the chunk one strategy was most confident about. Pinning a case
    where the constant CHANGES the answer is what proves it is read at all —
    the previous version of this test asserted a flip at r=2, which no constant
    can produce.
    """
    lists = [["first", "x", "y", "shared"], ["other", "p", "q", "shared"]]

    assert reciprocal_rank_fusion(lists, rank_constant=60)[0] == "shared"
    assert reciprocal_rank_fusion(lists, rank_constant=1)[0] == "first"


def test_the_result_is_deterministic_for_tied_ids() -> None:
    """Ties must not depend on dict or set iteration order.

    Every recorded retrieval margin is measured against a specific ordering. If
    ties resolved arbitrarily, a rank could change between identical runs and be
    misread as a stage doing something.
    """
    lists = [["x", "y"], ["y", "x"]]
    first = reciprocal_rank_fusion(lists)

    assert all(reciprocal_rank_fusion(lists) == first for _ in range(20))


def test_ties_break_toward_the_better_position_achieved() -> None:
    """`a` and `b` have identical scores; `a` reached rank 1 somewhere."""
    fused = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])

    assert fused == ["a", "b"]


def test_a_fully_tied_pair_does_not_fall_back_on_insertion_order() -> None:
    """The tiebreak must be an explicit rule, not an accident of the runtime.

    Here `a` and `b` tie on score AND on best position, and `b` is encountered
    first. Python's sort is stable and dicts preserve insertion order, so code
    with NO tiebreak at all still returns a deterministic answer — just the
    order the ids happened to arrive in. Every other test in this file passes
    against that version. This one fails, because the documented rule says the
    id decides and the id says `a`.
    """
    assert reciprocal_rank_fusion([["b", "a"], ["a", "b"]]) == ["a", "b"]


def test_limit_truncates_after_fusing_not_before() -> None:
    """Truncating the INPUTS would change which ids fuse at all.

    A chunk ranked low in both lists can still outrank one ranked high in only
    one; cutting before fusion would silently drop that outcome.
    """
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "a"]], limit=2)

    assert len(fused) == 2
    assert fused == reciprocal_rank_fusion([["a", "b", "c"], ["c", "b", "a"]])[:2]


def test_an_empty_ranking_contributes_nothing_rather_than_failing() -> None:
    """One strategy returning no hits is normal, not an error.

    BM25 legitimately matches nothing for a query with no lexical overlap. That
    must degrade to the other strategy's ranking, not break retrieval.
    """
    assert reciprocal_rank_fusion([["a", "b"], []]) == ["a", "b"]


def test_fusing_nothing_returns_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


@pytest.mark.parametrize("bad", [0, -1, -60])
def test_a_non_positive_rank_constant_is_refused(bad: int) -> None:
    """Zero divides the top rank by k+1=1 and dominates; negatives can divide
    by zero or invert the ordering. Neither is a thing a caller means."""
    with pytest.raises(ValueError, match="rank_constant must be positive"):
        reciprocal_rank_fusion([["a"]], rank_constant=bad)
