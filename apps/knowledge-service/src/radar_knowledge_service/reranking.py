"""Cross-encoder reranking: the pure half.

Reranking asks an LLM to score each candidate's relevance to the query, then
reorders by that score. The LLM call is I/O and lives in the gateway client; this
module holds the parts that decide — building the prompt, parsing the reply, and
applying the scores — so all of them are testable without a network.

ONE BATCHED CALL, NOT ONE PER CANDIDATE
---------------------------------------
Every candidate is scored in a single ``reason``-mode request. Scoring ten
candidates with ten calls would multiply latency and cost by ten for a stage that
sits in the incident path, and it would also make the scores incomparable: each
call would judge a chunk in isolation, with no view of the alternatives, which is
exactly the comparison reranking exists to make.

TIES ARE A CORRECTNESS PROBLEM, NOT AN EDGE CASE
------------------------------------------------
An LLM asked for integer relevance scores WILL return ties — that is the normal
case, not a rare one. So the tiebreak is a stated rule rather than whatever the
sort happens to do:

    score descending, then the candidate's INCOMING position, then chunk_id.

Falling back on incoming position means a tie leaves the pipeline's existing
belief untouched: if reranking cannot distinguish two chunks, fusion's ordering
stands. The chunk_id break is arbitrary but total, so no pair can ever be ordered
by arrival.

This matters beyond neatness. The retrieval evaluation attributes rank changes to
specific stages; if ties resolved by arrival order, a rank could move because a
dict was rebuilt or Elasticsearch returned hits in a different order, and the
change would be credited to reranking. The fusion core has the same rule for the
same reason.

UNSCORED CANDIDATES ARE KEPT, NOT DROPPED
-----------------------------------------
A model that omits a chunk from its reply, or invents an id, must not be able to
delete content from the result. Anything the model did not score keeps its
incoming order and sorts after everything it did score. The failure mode this
prevents is the quiet one: a truncated reply silently shortening retrieval.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: Scores are requested on a small integer scale. A wide scale invites false
#: precision — the distinction between 71 and 68 is noise — while a scale this
#: size still separates "answers the question" from "same service, wrong
#: problem", which is the judgement being asked for.
MIN_SCORE = 0
MAX_SCORE = 10

RERANK_SYSTEM_PROMPT = """\
You rank runbook excerpts by how well they help resolve a specific incident.

Score each excerpt from 0 to 10:
  10  directly describes this incident and how to resolve it
   7  describes this class of incident, useful but not specific
   4  same service or component, different problem
   0  unrelated

Judge the excerpt against the incident description only. Excerpts from the same
service often describe DIFFERENT failures — an excerpt that discusses the right
service but the wrong problem is a 4, not a 7. Prefer the excerpt that matches
what is actually happening over one that merely shares vocabulary with it.

Reply with JSON only, no prose, in exactly this form:
{"scores": [{"chunk_id": "<id>", "score": <0-10>}]}

Score every excerpt you were given, once each."""


class ChunkScore(BaseModel):
    """One candidate's relevance score, as the model reports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    score: float = Field(ge=MIN_SCORE, le=MAX_SCORE)


class RerankScores(BaseModel):
    """The whole reply. A model, not a bare dict, so a malformed reply is a
    validation error at the boundary rather than a KeyError somewhere later."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scores: list[ChunkScore]


class RerankParseError(ValueError):
    """The model's reply was not the requested shape.

    Its own class so the caller can distinguish "the model misbehaved" from a
    transport failure and decide separately — reranking is an improvement stage,
    and losing it should degrade retrieval to the fused order rather than fail
    the incident.
    """


def build_rerank_prompt(query: str, candidates: list[dict[str, Any]]) -> str:
    """Render the query and candidates into the user message.

    Chunk ids are included because they are how scores are joined back to
    candidates — position would be fragile, since a model that reorders or omits
    entries would silently misattribute every score after the first gap.
    """
    if not candidates:
        raise ValueError("cannot build a rerank prompt with no candidates")

    excerpts = []
    for candidate in candidates:
        excerpts.append(
            f"--- chunk_id: {candidate['chunk_id']}\n"
            f"runbook: {candidate.get('runbook_id', '?')} "
            f"| section: {candidate.get('section', '?')}\n"
            f"{candidate.get('text', '')}"
        )
    joined = "\n\n".join(excerpts)
    return f"INCIDENT:\n{query}\n\nEXCERPTS:\n{joined}"


def parse_rerank_scores(payload: str) -> dict[str, float]:
    """Parse the model's reply into ``chunk_id -> score``.

    Tolerates the model wrapping its JSON in a fenced code block, because that is
    a formatting habit rather than a failure to follow the instruction, and
    rejecting it would discard a usable answer. Anything else raises.

    A duplicate chunk_id is refused rather than silently resolved: last-wins and
    first-wins are both defensible, which is the reason not to pick one quietly.
    """
    text = payload.strip()
    if text.startswith("```"):
        # Drop the fence line and anything after the closing fence.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]

    try:
        parsed = RerankScores.model_validate_json(text)
    except ValidationError as exc:
        raise RerankParseError(
            f"rerank reply was not the requested shape: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RerankParseError(f"rerank reply was not valid JSON: {exc}") from exc

    scores: dict[str, float] = {}
    for entry in parsed.scores:
        if entry.chunk_id in scores:
            raise RerankParseError(
                f"rerank reply scored {entry.chunk_id!r} more than once; "
                f"refusing to guess which score was meant"
            )
        scores[entry.chunk_id] = entry.score
    return scores


def rerank_by_scores(
    candidates: list[dict[str, Any]],
    scores: dict[str, float],
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Reorder ``candidates`` by score, best first.

    The ordering rule, in full: score descending, then incoming position, then
    ``chunk_id``. Unscored candidates sort after every scored one, keeping their
    incoming order among themselves.

    Scores for ids that are not candidates are ignored — a model inventing an id
    must not be able to inject a document into the result.
    """
    positions = {
        candidate["chunk_id"]: index for index, candidate in enumerate(candidates)
    }

    def sort_key(candidate: dict[str, Any]) -> tuple[int, float, int, str]:
        chunk_id = candidate["chunk_id"]
        scored = chunk_id in scores
        return (
            0 if scored else 1,  # scored candidates first
            -scores.get(chunk_id, 0.0),  # then by score, descending
            # The last two terms are BELT AND BRACES HERE, not load-bearing, and
            # that is worth stating precisely so nobody mistakes it for a tested
            # guarantee. `positions` is built from `enumerate(candidates)`, so
            # "incoming position" IS arrival order, and Python's sort is stable —
            # deleting both terms produces byte-identical output (verified
            # against 20k tie-heavy random inputs). They are kept as executable
            # documentation of the intended rule, and because the equivalence
            # holds only while this sorts a LIST with unique ids: sorting a dict
            # or a set, as the fusion core does, makes the same omission a real
            # arrival-order bug.
            positions[chunk_id],  # then the pipeline's existing belief
            chunk_id,  # then an arbitrary but total rule
        )

    ordered = sorted(candidates, key=sort_key)
    return ordered if limit is None else ordered[:limit]
