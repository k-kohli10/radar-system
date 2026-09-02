"""CRAG grading: the pure half.

Retrieval ranking says which chunks are CLOSEST, never whether the closest is
CLOSE ENOUGH. With a `services` pre-filter, an inventory incident retrieves
inventory runbooks whether or not any of them describes what is happening. CRAG
asks that second question, so the reasoner can be told "nothing here helps"
instead of being handed the least-bad wrong answer.

THE STAGE ONLY EARNS ITS PLACE IF IT CAN RETURN NOTHING
--------------------------------------------------------
A grader that always answers "sufficient" costs an LLM call per incident and
changes no decision. So the property that matters is the negative one: an
incident whose service has runbooks but none that fit must come back with an
EMPTY context. That is what :func:`apply_grades` guarantees when every chunk
grades insufficient, and it is the case the tests exercise.

ONE BATCHED CALL
----------------
Every chunk is graded in a single ``reason``-mode request. Per-chunk calls would
multiply cost and latency by the number of chunks, and would judge each chunk
with no view of the alternatives, when "is this the best of a bad set" is part
of the judgement.

UNGRADED CHUNKS MEAN THE REPLY IS UNUSABLE
-------------------------------------------
If the model omits a chunk, this module refuses the whole reply rather than
guessing. Treating an omission as insufficient lets a truncated reply silently
empty the context; treating it as sufficient lets an ungraded chunk reach the
reasoner wearing a grade it was never given. The caller degrades to ungraded
retrieval instead.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class Grade(StrEnum):
    """How well a chunk supports the incident.

    Three levels rather than a score: the decision this feeds is ternary (use
    it, use it with caution, ignore it), and a 0-10 scale would invite arguing
    about whether a 6 is usable.

    ``insufficient`` decides inclusion (:data:`USABLE`), so the grading call is
    load-bearing. ``sufficient`` versus ``partial`` does not reach the reasoner's
    prompt: instructing a model to prefer ``sufficient`` excerpts backfires on an
    all-``partial`` bundle, reading as "all of your context is the weaker kind"
    and driving an empty-context RCA with the right runbook in hand. See
    ``radar_reasoner_agent.llm``.

    The pair is kept for the audit trail. The grader demonstrably discriminates
    (27 ``partial`` to 12 ``sufficient`` across every chunk stored, both grades
    often within one bundle), so the signal exists; the open question is how to
    give it to the model without the framing that misfired.
    """

    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


#: Grades whose chunks reach the reasoner. `partial` is included deliberately:
#: a runbook for a related failure in the same service is genuine context for an
#: engineer, and dropping it would leave the reasoner with nothing in exactly the
#: ambiguous cases where some grounding beats none.
USABLE = (Grade.SUFFICIENT, Grade.PARTIAL)

CRAG_SYSTEM_PROMPT = """\
You judge whether runbook excerpts actually help resolve a specific incident.

Grade each excerpt:
  sufficient    describes THIS incident and how to resolve it
  partial       related — same failure class, or useful background — but does
                not describe this specific incident
  insufficient  does not help with this incident

Excerpts are pre-filtered to the incident's service, so they will all LOOK
relevant. Being about the right service is not enough: an excerpt describing a
DIFFERENT failure of the same service is `insufficient`, not `partial`, unless
it genuinely informs this one.

If none of the excerpts help, grade them all `insufficient`. That is a useful
answer, not a failure — it tells the engineer the runbooks do not cover this.

Reply with JSON only, no prose, in exactly this form:
{"grades": [{"chunk_id": "<id>", "grade": "sufficient|partial|insufficient"}]}

Grade every excerpt you were given, once each."""


class ChunkGrade(BaseModel):
    """One chunk's grade, as the model reports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    grade: Grade


class CragGrades(BaseModel):
    """The whole reply, validated at the boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grades: list[ChunkGrade]


class CragParseError(ValueError):
    """The model's reply was not the requested shape, or did not cover every
    chunk. Its own class so the caller can degrade rather than fail the
    incident."""


def build_grading_prompt(query: str, chunks: list[dict[str, Any]]) -> str:
    """Render the incident and its retrieved chunks into the user message."""
    if not chunks:
        raise ValueError("cannot build a grading prompt with no chunks")

    excerpts = [
        f"--- chunk_id: {chunk['chunk_id']}\n"
        f"runbook: {chunk.get('runbook_id', '?')} "
        f"| section: {chunk.get('section', '?')}\n"
        f"{chunk.get('text', '')}"
        for chunk in chunks
    ]
    return f"INCIDENT:\n{query}\n\nEXCERPTS:\n" + "\n\n".join(excerpts)


def parse_grades(payload: str, expected_ids: list[str]) -> dict[str, Grade]:
    """Parse the reply into ``chunk_id -> Grade``, requiring full coverage.

    ``expected_ids`` is checked rather than trusted: a reply that grades three of
    five chunks is unusable, because the two ungraded chunks would have to be
    guessed at. Unknown ids are also refused, since a grade for a chunk that was
    never sent means the model is not answering about the excerpts it was given.
    """
    text = payload.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]

    try:
        parsed = CragGrades.model_validate_json(text)
    except ValidationError as exc:
        raise CragParseError(
            f"grading reply was not the requested shape: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CragParseError(f"grading reply was not valid JSON: {exc}") from exc

    grades: dict[str, Grade] = {}
    for entry in parsed.grades:
        if entry.chunk_id in grades:
            raise CragParseError(
                f"grading reply graded {entry.chunk_id!r} more than once"
            )
        grades[entry.chunk_id] = entry.grade

    missing = [chunk_id for chunk_id in expected_ids if chunk_id not in grades]
    if missing:
        raise CragParseError(
            f"grading reply covered {len(grades)} of {len(expected_ids)} chunks; "
            f"ungraded: {missing}. An incomplete reply is unusable — the missing "
            f"grades would have to be guessed, and guessing either way is wrong."
        )
    unknown = set(grades) - set(expected_ids)
    if unknown:
        raise CragParseError(
            f"grading reply graded chunks that were never sent: {sorted(unknown)}"
        )
    return grades


def apply_grades(
    chunks: list[dict[str, Any]], grades: dict[str, Grade]
) -> list[dict[str, Any]]:
    """Keep the usable chunks, in their incoming order, each carrying its grade.

    Returns an EMPTY list when every chunk grades insufficient. That is the
    stage's reason for existing: the reasoner is told the corpus has nothing for
    this incident, rather than being handed the closest available wrong answer
    and grounding an RCA in it.

    Order is retrieval's, not the grader's. Reranking by grade was measured and
    removed from this pipeline; CRAG's job is to decide what is usable, not to
    re-decide what is best.
    """
    kept = []
    for chunk in chunks:
        grade = grades.get(chunk["chunk_id"])
        if grade in USABLE:
            kept.append({**chunk, "grade": str(grade)})
    return kept
