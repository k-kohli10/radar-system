"""Parse the model's answer into an RCA — or fail, entirely.

The model was *asked* for a JSON object. What it *returns* is another matter, and this
module is where "it said something plausible but unusable" becomes a typed failure the
fallback can act on rather than a half-built recommendation nobody notices is wrong.

ALL OR NOTHING
--------------
The dangerous middle ground is a response that parses far enough to populate
``root_cause`` and then falls over on ``recommended_actions``. A parser that walked the
fields one at a time would store the half it liked: a confident root-cause paragraph
with an empty action list. The engineer reads a card that says what broke and tells them
to do nothing. That is worse than the template fallback, which at least hands them the
investigation checklist — and it is worse *quietly*, because the row looks fine.

So the response is validated as ONE object, in one call. There is no partial success to
guard against because there is no code path that could produce one: either
:class:`ParsedRCA` validates completely, or nothing is returned at all and the caller
falls back. The property is structural, not vigilant.

WHERE THE LINE IS
-----------------
Strict about what we REQUIRE, liberal about what we TOLERATE. The goal is a usable RCA
whenever the model produced one — falling back is never *wrong*, but it costs a real
analysis, and a fallback rate driven by pedantry is a self-inflicted wound.

Tolerated (extracted or normalized, then validated like anything else):

- **Markdown fences.** Models wrap JSON in ```json blocks constantly, whatever the
  prompt says. Stripped.
- **Prose either side of the object.** The outermost ``{…}`` is extracted. This cannot
  smuggle anything past validation — whatever comes out is still validated in full, so
  the worst case is the same failure we would have had anyway.
- **Capitalized confidence.** ``"High"`` means high. Falling back over a capital letter
  would be absurd.
- **Extra keys.** A model that adds ``"reasoning": "..."`` has still answered the
  question. The fields we depend on are all present; the rest is ignored.

Refused (each a complete failure, and the fallback takes over):

- Anything that is not a JSON object once extracted.
- A missing or empty ``root_cause``.
- A ``confidence`` outside the closed vocabulary — ``"very high"`` is not a value we
  know how to compare, and guessing which of low/medium/high it meant would be
  inventing data.
- **Zero recommended actions.** An RCA with no actions is useless to an engineer at
  3am, and the fallback is strictly BETTER — it carries the plan's real investigation
  steps. Falling back here loses nothing and gains a usable card.
- Any malformed action (no order, no text, order <= 0).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from radar_common import get_logger
from radar_contracts import Confidence, RecommendedAction

log = get_logger("reasoner.rca")

#: ```json … ``` or ``` … ``` — the fence models add no matter what the prompt says.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)

_UNREADABLE = object()
"""Sentinel: json.loads could not read this.

Not ``None`` — ``json.loads("null")`` legitimately RETURNS None, and conflating "the
model sent null" with "the text was unreadable" would send one down the wrong path.
"""


class RCAParseFailureReason(StrEnum):
    """WHY the answer was unusable. Recorded on the fallback's context bundle."""

    #: Nothing that could be read as a JSON object came back — prose, an apology, an
    #: empty string, a truncated stream.
    NOT_JSON = "not_json"

    #: It was JSON, and it was not the JSON we asked for: a missing field, an unknown
    #: confidence, an empty action list, a malformed action. The model answered; the
    #: answer is not usable.
    SCHEMA_INVALID = "schema_invalid"


class ParsedRCA(BaseModel):
    """A complete, usable root-cause analysis. There is no incomplete one.

    ``extra`` is left at Pydantic's default (ignore) DELIBERATELY, against this repo's
    usual ``extra="forbid"`` discipline. That rule exists for data WE produce, where an
    unexpected key means one of our own components has drifted. This is a language
    model's output: an extra key means it was chatty, not that anything is broken, and
    discarding a good analysis over it would trade a real RCA for a template.
    """

    model_config = ConfigDict(frozen=True)

    root_cause: str = Field(min_length=1)
    confidence: Confidence
    #: At least one. Zero actions is a failure, not an empty success — see the module
    #: docstring.
    recommended_actions: list[RecommendedAction] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class RCAParseFailure:
    """The answer could not be used. The caller falls back, entirely."""

    reason: RCAParseFailureReason
    detail: str


RCAResult = ParsedRCA | RCAParseFailure


def parse_rca(content: str) -> RCAResult:
    """Turn the model's raw text into an RCA, or into a typed failure. Never raises.

    One validation call over the whole object: there is no path that returns a partially
    populated RCA, because there is no code that builds one field at a time.
    """
    document = _extract_json_object(content)
    if document is None:
        return _fail(
            RCAParseFailureReason.NOT_JSON,
            "the model did not return a JSON object",
        )

    try:
        # THE all-or-nothing line. One object, one call. A response that satisfies
        # root_cause and fails on recommended_actions produces NOTHING — not a
        # confident paragraph with an empty action list.
        return ParsedRCA.model_validate(document)
    except ValidationError as exc:
        return _fail(
            RCAParseFailureReason.SCHEMA_INVALID,
            f"the model's JSON does not match the RCA schema: "
            f"{exc.error_count()} field error(s)",
        )


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """Pull a JSON object out of whatever the model actually sent back.

    Deterministic, and safe by construction: anything this returns is still validated in
    full afterwards, so a bad extraction cannot smuggle a half-formed RCA through — it
    just produces the same failure we would have had.
    """
    text = content.strip()
    if not text:
        return None

    fenced = _FENCE.match(text)
    if fenced is not None:
        text = fenced.group("body").strip()

    document = _load(text)
    if document is _UNREADABLE:
        # Prose around the object — before it ("Here is the analysis: {…}"), AFTER it
        # ("{…} Let me know if you need more detail."), or both. Take the outermost
        # braces and try again.
        #
        # Checking `startswith("{")` instead would miss the trailing case entirely: the
        # text starts with the object, json.loads chokes on the pleasantry after it, and
        # a perfectly good RCA is thrown away over a closing courtesy. Try the whole
        # thing first, THEN carve.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        document = _load(text[start : end + 1])

    if not isinstance(document, dict):
        # Unreadable, or a JSON array / bare string / number / null. Valid JSON in some
        # cases; an RCA in none of them.
        return None

    return _normalize(document)


def _load(text: str) -> Any:
    """``json.loads``, returning :data:`_UNREADABLE` instead of raising."""
    try:
        return json.loads(text)
    except ValueError:
        # json.JSONDecodeError subclasses ValueError, which also covers the odd
        # non-JSON input json.loads rejects outright.
        return _UNREADABLE


def _normalize(document: dict[str, Any]) -> dict[str, Any]:
    """The only coercion allowed: the case of ``confidence``.

    ``"High"`` means high, and falling back over a capital letter would be absurd. But
    ``"very high"`` still fails — lowercasing does not make it a value we know how to
    compare, and guessing which of low/medium/high it meant would be inventing data.
    """
    confidence = document.get("confidence")
    if isinstance(confidence, str):
        return {**document, "confidence": confidence.strip().lower()}
    return document


def _fail(reason: RCAParseFailureReason, detail: str) -> RCAParseFailure:
    log.warning("rca.unusable", reason=reason.value, detail=detail)
    return RCAParseFailure(reason, detail)
