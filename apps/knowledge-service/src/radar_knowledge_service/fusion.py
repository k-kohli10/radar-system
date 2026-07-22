"""Reciprocal rank fusion: combine several ranked lists into one.

WHY THIS IS HAND-WRITTEN
------------------------
Elasticsearch ships a native ``rrf`` retriever, and this cluster cannot use it:
the deployment runs a **basic** licence, and RRF is a licensed feature — the
query is rejected with ``current license is non-compliant for [Reciprocal Rank
Fusion (RRF)]``. So the choice was made for us.

It is also the choice we would have made. Fusion is the stage where a
pre-registered miss may get fixed (see ``tests/retrieval/probes.yaml``), and the
whole retrieval evaluation is built on attributing a margin or rank change to a
specific stage. A stage that is a black box configured through a vendor query
cannot be attributed, only guessed at. This is thirty lines of pure function,
exhaustively testable with no infrastructure, and it fits the pattern the chunker
and reconciliation established: decide in a pure core, do I/O in a thin shell.

WHY RANKS AND NOT SCORES
------------------------
BM25 scores are unbounded and corpus-dependent; cosine similarity is bounded in
[-1, 1]. They cannot be added, averaged, or compared without inventing a
normalisation that no evidence supports — and the invented constant would then be
silently doing the retrieval quality work. RRF sidesteps this by throwing away
the scores entirely and using only position, which is the one thing both lists
express in the same units.

The cost is real and worth stating: fusion cannot tell a landslide from a photo
finish. A chunk that wins its list by a mile and one that wins by 0.0001 both
contribute ``1/(k+1)``. That is why ``tests/retrieval/baseline.json`` keeps
recording the cosine margin on the kNN leg separately — the magnitude the fusion
discards is exactly what says whether a change was decisive or a coin flip.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: The RRF damping constant from Cormack et al., and the value Elasticsearch's
#: own implementation defaults to. It flattens the difference between the top
#: positions: with k=60, ranks 1 and 2 contribute 1/61 and 1/62 — close together,
#: so a single list cannot dominate the fusion by being confident. Lower k makes
#: the top of each list sharper and the fusion more winner-take-all.
DEFAULT_RANK_CONSTANT = 60


def reciprocal_rank_fusion(
    rankings: Iterable[Sequence[str]],
    *,
    rank_constant: int = DEFAULT_RANK_CONSTANT,
    limit: int | None = None,
) -> list[str]:
    """Fuse ranked id lists into one ranking, best first.

    Each list contributes ``1/(rank_constant + position)`` to every id it
    contains, counting positions from 1. Ids are then ordered by total
    contribution, so appearing respectably in several lists beats topping one.

    Ties are broken by best position achieved in any list, then by id. Both
    tiebreaks exist to make the output a deterministic function of the input:
    an ordering that depends on dict iteration or set order would make the
    retrieval evaluation unreproducible, and every margin recorded against it
    meaningless. The id tiebreak is arbitrary but stable, which is the property
    that matters.

    Raises on a non-positive ``rank_constant``: zero would make the top of each
    list infinitely dominant, negatives produce a division by zero at some rank
    or silently invert the ordering. None of those are things a caller means.
    """
    if rank_constant <= 0:
        raise ValueError(
            f"rank_constant must be positive, got {rank_constant}. Zero or "
            f"negative makes the fusion either winner-take-all or inverted."
        )

    scores: dict[str, float] = {}
    best_position: dict[str, int] = {}

    for ranking in rankings:
        for position, identifier in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (
                rank_constant + position
            )
            if position < best_position.get(identifier, position + 1):
                best_position[identifier] = position

    fused = sorted(
        scores,
        key=lambda identifier: (
            -scores[identifier],
            best_position[identifier],
            identifier,
        ),
    )
    return fused if limit is None else fused[:limit]
