"""The template fallback, and the total match that decides when it is used.

This is the module the whole service exists for. Everything before it — the gateway
client, the parser — is machinery for getting a good RCA. This is the part that
guarantees the engineer gets *an* RCA.

THE INVARIANT: AN INCIDENT ALWAYS ENDS WITH A RECOMMENDATION
------------------------------------------------------------
Not when the provider is down. Not when the call outruns its budget. Not when our own
token is rejected. Not when the model answers with an apology instead of JSON. Every
one of those ends the same way: a recommendation built from the plan's own
investigation steps, marked ``is_fallback=true``, and the engineer still gets a card
telling them what to check.

FALLBACK IS NOT TRIGGERED BY A LIST OF REASONS
----------------------------------------------
It is triggered by **the absence of a clean success**, which is a different and much
safer thing. A trigger list — "fall back on 503, on timeout, on bad JSON" — is a list
somebody has to keep complete, and the day a new failure mode is added is the day the
list is silently wrong and an incident falls through it with no RCA at all.

So there is no list. :func:`resolve` matches over the whole of ``LLMSuccess |
LLMFailure`` and, inside the success arm, the whole of ``ParsedRCA | RCAParseFailure``.
Exactly ONE path produces a real recommendation — a call that succeeded *and* parsed.
Every other path in the type space produces a template. The matches end in
:func:`typing.assert_never`, so **adding a failure mode without deciding what it means
is a type error**, not a production surprise: mypy fails the build, and the missing
case is named in the error.

``fallback_reason`` RECORDS which failure happened. It does not DECIDE anything. That
distinction is the point — nothing branches on it, so nothing can forget a value of it.

The reasons are deliberately re-declared as :class:`FallbackReason` rather than reusing
``LLMFailureReason`` and ``RCAParseFailureReason`` directly, and the mapping is an
explicit exhaustive match rather than ``FallbackReason(failure.reason.value)``. The
string values are identical, so the shortcut would work — right up until someone adds a
fourth ``LLMFailureReason``, at which point the shortcut raises ``ValueError`` at 3am on
a live incident, while the match refuses to compile. The coupling is made visible on
purpose.

ONE GENERATOR, NOT THREE
------------------------
Every trigger calls the same :func:`generate_template_rca`. Three copies of "build an
RCA from the plan steps" would drift, and the one that drifted would be the one nobody
tested — the rare path, which is exactly the path that only runs when things are
already going badly.

WHY THE GENERATOR CANNOT RAISE
------------------------------
:class:`~radar_reasoner_agent.rca.ParsedRCA` requires **at least one** recommended
action. The plan's steps come from a JSONB column, and ``ContextBundle`` puts no floor
on them — so an empty ``steps`` array is representable, and a generator that mapped
steps to actions one-for-one would raise ``ValidationError`` on it.

That would be a spectacular failure: the fallback, whose entire job is to be the thing
that cannot fail, throwing an exception, dead-lettering the event, and leaving the
incident with no recommendation — the exact outcome this module exists to prevent, and
reached *through* the code that prevents it.

So the generator guarantees an action floor by construction (:data:`NO_STEPS_ACTION`).
The plan template loader already enforces ``min_length=1`` on steps, so this should be
unreachable — "should be" is not a property, and the invariant is not allowed to rest
on another service's validator holding.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from pydantic import BaseModel, ConfigDict
from radar_common import get_logger
from radar_contracts import Confidence, LLMMode, RecommendedAction

from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.llm import (
    LLMFailure,
    LLMFailureReason,
    LLMResult,
    LLMSuccess,
)
from radar_reasoner_agent.rca import (
    ParsedRCA,
    RCAParseFailure,
    RCAParseFailureReason,
    parse_rca,
)

log = get_logger("reasoner.fallback")

ATTEMPTED_MODE = LLMMode.EXTENDED
"""The mode the reasoner asks for. Recorded on every outcome, including fallbacks.

A fallback row says ``model_alias="none"``, which is true — no model ran — but it
discards the fact that we *tried* to spend an extended-mode call. That belongs in the
audit trail, so it goes in the context bundle where it cannot contaminate a
``GROUP BY model_alias``.
"""

FALLBACK_PROVIDER = "none"
"""No provider served this. NOT the provider we tried — none of them answered."""

FALLBACK_MODEL_ALIAS = "none"
"""NOT ``"extended"``. No model ran, and a ``GROUP BY model_alias`` must not count
template fallbacks as extended-mode traffic."""

FALLBACK_MODEL_ID = "template-fallback"
"""A model id that is obviously not a model. Anyone reading the row sees what it is."""

NO_STEPS_ACTION = (
    "No investigation plan steps are available for this incident. Triage manually: "
    "check recent deployments, error logs, and dependency health for the service."
)
"""The action floor. See "WHY THE GENERATOR CANNOT RAISE" in the module docstring.

Generic, and unapologetically so — it is what there is to say when the plan we were
meant to hand over is empty. A generic action beats a dead-lettered incident.
"""


class FallbackReason(StrEnum):
    """WHY this incident got a template instead of an analysis.

    Recorded, never branched on. Five values across two very different human
    responses, and keeping them apart is the point of having an enum rather than a
    boolean:

    - ``gateway_unavailable`` / ``timeout`` — the LLM is down or slow. Wait, or page
      whoever owns the provider budget. RADAR is fine.
    - ``rejected`` — **our** misconfiguration: a bad token, a mode this token may not
      use, a request the gateway will not accept. It will fail identically on every
      incident until a human fixes the config. A steady rate of this means the reasoner
      has never once used the LLM, while looking perfectly healthy.
    - ``not_json`` / ``schema_invalid`` — the model answered, and the answer was
      unusable. A prompt or model-quality problem, not an outage.

    Alerting cannot tell those apart from a single counter, which is why this value is
    a metric label and not just a log line.
    """

    #: Every provider failed (gateway 503), the gateway itself errored, or we could not
    #: reach it at all.
    GATEWAY_UNAVAILABLE = "gateway_unavailable"

    #: The call outran the reasoner's budget. The LLM is UP and slow — a capacity
    #: problem, not an outage.
    TIMEOUT = "timeout"

    #: The gateway rejected US: 401, 403, or 422. Our config is broken.
    REJECTED = "rejected"

    #: Nothing that could be read as a JSON object came back.
    NOT_JSON = "not_json"

    #: It was JSON, and it was not the JSON we asked for.
    SCHEMA_INVALID = "schema_invalid"


class FallbackMetadata(BaseModel):
    """Why we fell back, and what we had been trying to do. Stored, not prompted.

    Lives in ``recommendations.context_bundle``, alongside the bundle the model was (or
    would have been) shown. It is the only durable record of *why* a row is a template
    — the row's own columns say ``provider="none"``, which says a model did not answer
    but not what went wrong.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: FallbackReason
    #: What we asked for, even though nothing served it. See :data:`ATTEMPTED_MODE`.
    attempted_mode: LLMMode
    #: The failure's own words — "HTTP 503", "the model's JSON does not match the RCA
    #: schema: 2 field error(s)". Never the raw model text; that goes in
    #: ``raw_llm_response``, which is a column.
    detail: str
    #: How long we waited before giving up. ``None`` is not "instant" — it is "we never
    #: got as far as timing anything".
    elapsed_ms: int | None = None


class RetrievalMetadata(BaseModel):
    """How the bundle's ``retrieved_context`` came to be. Stored, not prompted.

    Phase 8: retrieval has THREE outcomes, and two of them produce an identical
    empty ``retrieved_context`` on the bundle. This sibling record is what keeps
    them apart in the audit trail:

    - ``grounded``     chunks were retrieved and graded; the model saw them.
    - ``empty``        CRAG judged nothing in the corpus relevant. The RCA is
                       honestly ungrounded — a claim about COVERAGE.
    - ``unavailable``  the knowledge service could not answer. The RCA is
                       ungrounded because retrieval FAILED — a claim about an
                       OUTAGE, and treating it as the first would be a lie.

    ``None`` on the wrapper means retrieval was never attempted (the knowledge
    service is not configured) — pre-Phase-8 behaviour, still supported.

    Like ``FallbackMetadata`` it lives OUTSIDE the bundle: the bundle is
    serialized straight into the prompt, and this is bookkeeping the model has
    no business reading.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    #: How many chunks the model was shown. Zero for empty and unavailable.
    chunk_count: int = 0
    #: Failure class or HTTP status when unavailable; never a vendor message.
    detail: str | None = None
    elapsed_ms: int | None = None


class StoredContextBundle(BaseModel):
    """What lands in ``recommendations.context_bundle``.

    A wrapper that COMPOSES rather than extends: the prompt-facing
    :class:`~radar_reasoner_agent.context.ContextBundle` is nested **verbatim**, with
    our own metadata in a sibling key.

    It is the bundle verbatim, not the prompt verbatim, and the two stopped being the
    same thing when the grade leak was fixed. ``retrieved_context`` chunks are stored
    with their ``grade`` and ``status``; the model is shown a projection that drops
    both (``radar_reasoner_agent.llm.PROMPTED_CHUNK_FIELDS``). So this column is a
    SUPERSET of what was prompted — deliberately, since a grade the model never saw is
    exactly the kind of thing an auditor needs and the model does not. Reconstructing
    the literal prompt from a stored row means replaying that projection.

    Flattening the wrapper into one object would be prettier and would quietly destroy
    the only thing this column is for. The bundle exists so a human can reconstruct
    what the model was working from; merged with fields that are pure bookkeeping,
    "what was retrieved" and "what we concluded about the retrieval" become
    indistinguishable, and the audit record is no longer an audit record.

    It is also why ``fallback_reason`` is not simply a field on ``ContextBundle``:
    that object is the *input to the prompt renderer*, and a fallback field on it
    would be a field the model reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: What the model was working from — or would have been, if the call never landed.
    #: A superset of the prompt: see the class docstring on the chunk projection.
    bundle: ContextBundle
    #: ``None`` on a real analysis. Present on every fallback.
    fallback: FallbackMetadata | None = None
    #: ``None`` when retrieval was never attempted. See :class:`RetrievalMetadata`.
    retrieval: RetrievalMetadata | None = None


@dataclass(frozen=True, slots=True)
class ReasoningOutcome:
    """One recommendation, ready to store. There is always exactly one.

    Every field the ``recommendations`` row needs, decided here so that storage (R7) is
    a mapping and not a second set of judgement calls. The two constructors below are
    the only ways to build one, and between them they cover the entire result space —
    so "we produced a recommendation" is true by construction, before anything is
    written.
    """

    root_cause: str
    confidence: Confidence
    recommended_actions: list[RecommendedAction]
    context_bundle: StoredContextBundle

    #: The self-describing half of the row. ``is_fallback`` and ``llm_provider`` are
    #: set together, in one place, on each path — they cannot disagree because there is
    #: no code that sets one without the other.
    is_fallback: bool
    llm_provider: str
    model_alias: str
    model_id: str

    # ---- The four columns that describe THE CALL, not the recommendation. ----
    #
    # All four are ``None`` together or populated together, because all four are read
    # off the same ``LLMSuccess | None`` in :func:`_from_failure`. There is no code that
    # can set one without the others, so a row cannot claim the model returned text
    # while reporting that it cost nothing.
    #
    # They mean ONE thing: **a call completed**. Not "a call succeeded" — a call that
    # came back with unusable garbage still ran, still took eight seconds, and OpenAI
    # still charged us for it. NULLing those true values on a fallback row would
    # silently under-report the only spend RADAR has, and the reasoner is the only
    # stage that spends anything.
    #
    # The worry this trades against — that fallback latencies pollute a p95 of real
    # analyses — is already answered by a column the row carries: ``is_fallback``.
    #   WHERE is_fallback = false  -> p95 of successful analyses
    #   WHERE is_fallback = true   -> how long we waited before giving up
    # Storing a true value and letting the reader filter beats destroying it to
    # pre-simplify an aggregate that can already be separated at query time. Same
    # principle as ``raw_llm_response``.
    #
    # NULL therefore means exactly one thing, and it is a fact rather than a choice:
    # **no call completed**. 503 before a response, a timeout, a rejected request — none
    # of them produce an ``LLMSuccess``, so none of them have usage to report.

    #: What the model literally returned. ``None`` only when it returned nothing.
    raw_llm_response: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None


def resolve(
    bundle: ContextBundle,
    result: LLMResult,
    *,
    retrieval: RetrievalMetadata | None = None,
) -> ReasoningOutcome:
    """Turn whatever happened into the one recommendation this incident will get.

    ``retrieval`` records how the bundle's ``retrieved_context`` came to be and
    rides through to the stored wrapper untouched — it is bookkeeping, and no
    branch below reads it.

    Total over the result space, and that totality is the invariant. There is one path
    to a real analysis — the call succeeded AND the answer parsed — and every other
    path in the type space lands on the template. Neither ``LLMResult`` nor
    ``RCAResult`` can carry an exception, so there is nothing to leak past this match:
    the guarantee is structural, not vigilant.
    """
    match result:
        case LLMSuccess():
            parsed = parse_rca(result.content)
            match parsed:
                case ParsedRCA():
                    # THE one path that does not fall back.
                    return _from_analysis(bundle, result, parsed, retrieval=retrieval)
                case RCAParseFailure():
                    # The model answered, and the answer was unusable. The CALL still
                    # happened: hand the whole LLMSuccess over, so the raw text, the
                    # tokens it cost and the time it took are all recorded together.
                    return _from_failure(
                        bundle,
                        reason=_reason_for_parse_failure(parsed.reason),
                        detail=parsed.detail,
                        call=result,
                        elapsed_ms=result.latency_ms,
                        retrieval=retrieval,
                    )
                case _:
                    assert_never(parsed)
        case LLMFailure():
            # Down, slow, or refusing us. No call COMPLETED, so there is no response
            # body, no token cost and no latency to report — `call=None` makes all four
            # of those NULL at once, because they are the same fact.
            return _from_failure(
                bundle,
                reason=_reason_for_llm_failure(result.reason),
                detail=result.detail,
                call=None,
                elapsed_ms=result.elapsed_ms,
                retrieval=retrieval,
            )
        case _:
            assert_never(result)


def generate_template_rca(bundle: ContextBundle) -> ParsedRCA:
    """The template RCA: the incident's own investigation plan, as recommended actions.

    THE single generator every trigger uses. It cannot raise — see the module docstring
    — because the one thing that could make it raise (a plan with no steps) is handled
    by construction rather than assumed away.

    The wording says the analysis is unavailable and does NOT say why. The plan's
    original snippet said "due to LLM provider outage", which is a lie on three of the
    five reasons — the provider is emphatically not down when it returns prose instead
    of JSON, or when it rejects our token. A card that misdiagnoses its own failure
    sends the engineer to check the wrong thing. The reason is recorded precisely, in
    :class:`FallbackMetadata`, where it is true.
    """
    actions = [
        RecommendedAction(order=step.order, action=step.description)
        for step in sorted(bundle.investigation_steps, key=lambda s: s.order)
    ]
    if not actions:
        # The floor. A generic action beats a dead-lettered incident.
        log.warning(
            "fallback.plan_has_no_steps",
            incident_id=str(bundle.incident_id),
            detail="the plan has no investigation steps; using generic triage",
        )
        actions = [RecommendedAction(order=1, action=NO_STEPS_ACTION)]

    return ParsedRCA(
        root_cause=(
            f"AI analysis unavailable. Manual investigation required for "
            f"{bundle.service_name} {bundle.alert_name}. The recommended actions "
            f"below are this incident's investigation plan steps, not a model's "
            f"analysis."
        ),
        # Never anything else. A template is a checklist, not an assessment, and it has
        # no business claiming it is confident.
        confidence=Confidence.LOW,
        recommended_actions=actions,
    )


def _from_analysis(
    bundle: ContextBundle,
    success: LLMSuccess,
    parsed: ParsedRCA,
    *,
    retrieval: RetrievalMetadata | None,
) -> ReasoningOutcome:
    """A real analysis: a model answered, and the answer was usable."""
    log.info(
        "reasoning.analysed",
        incident_id=str(bundle.incident_id),
        provider=success.provider,
        model=success.model,
        confidence=parsed.confidence.value,
        actions=len(parsed.recommended_actions),
        latency_ms=success.latency_ms,
    )
    return ReasoningOutcome(
        root_cause=parsed.root_cause,
        confidence=parsed.confidence,
        recommended_actions=list(parsed.recommended_actions),
        context_bundle=StoredContextBundle(
            bundle=bundle, fallback=None, retrieval=retrieval
        ),
        is_fallback=False,
        # A real provider, and never "none" — the agreement test in both directions
        # depends on these two being set together, here, and nowhere else.
        llm_provider=success.provider,
        model_alias=success.mode,
        model_id=success.model,
        raw_llm_response=success.content,
        prompt_tokens=success.prompt_tokens,
        completion_tokens=success.completion_tokens,
        latency_ms=success.latency_ms,
    )


def _from_failure(
    bundle: ContextBundle,
    *,
    reason: FallbackReason,
    detail: str,
    call: LLMSuccess | None,
    elapsed_ms: int | None,
    retrieval: RetrievalMetadata | None = None,
) -> ReasoningOutcome:
    """The template path. Every trigger lands here, differing only in ``reason``.

    ``call`` is the ONE input that decides all four call-describing columns:

    - ``LLMSuccess`` — the model ran and answered; the answer was just unusable. It
      cost tokens and it took time, and both are recorded. The row is a template, and
      the spend was real.
    - ``None`` — no call completed (503, timeout, rejected). Nothing to record.

    Passing the whole result rather than four separate arguments is what makes the
    columns unable to disagree: there is no call site that could supply the raw text and
    forget the tokens, because there is no argument for the raw text.
    """
    template = generate_template_rca(bundle)

    # WARNING, not error: the incident still gets a usable card, and a provider outage
    # is not a RADAR bug. REJECTED is the one that means something here is broken, and
    # it is told apart by the label, not by the level.
    log.warning(
        "reasoning.fell_back",
        incident_id=str(bundle.incident_id),
        reason=reason.value,
        detail=detail,
        attempted_mode=ATTEMPTED_MODE.value,
        elapsed_ms=elapsed_ms,
        actions=len(template.recommended_actions),
        # Spend that bought nothing. Zero on the paths where no call completed — which
        # is the number that makes a wasted-cost query answerable at all.
        wasted_prompt_tokens=call.prompt_tokens if call else 0,
    )
    return ReasoningOutcome(
        root_cause=template.root_cause,
        confidence=template.confidence,
        recommended_actions=list(template.recommended_actions),
        context_bundle=StoredContextBundle(
            bundle=bundle,
            fallback=FallbackMetadata(
                reason=reason,
                attempted_mode=ATTEMPTED_MODE,
                detail=detail,
                elapsed_ms=elapsed_ms,
            ),
            retrieval=retrieval,
        ),
        is_fallback=True,
        # Set together with is_fallback, in one place. A row cannot claim to be a
        # fallback while naming a provider, because no code path can build one.
        llm_provider=FALLBACK_PROVIDER,
        model_alias=FALLBACK_MODEL_ALIAS,
        model_id=FALLBACK_MODEL_ID,
        # All four from the same optional. Populated together, NULL together — the row
        # cannot say the model returned text while reporting that it cost nothing.
        raw_llm_response=call.content if call else None,
        prompt_tokens=call.prompt_tokens if call else None,
        completion_tokens=call.completion_tokens if call else None,
        latency_ms=call.latency_ms if call else None,
    )


def _reason_for_llm_failure(reason: LLMFailureReason) -> FallbackReason:
    """Map the call's failure onto the recorded reason. Exhaustive on purpose.

    A new ``LLMFailureReason`` must fail the build here, not silently acquire whichever
    fallback reason happens to share its string.
    """
    match reason:
        case LLMFailureReason.GATEWAY_UNAVAILABLE:
            return FallbackReason.GATEWAY_UNAVAILABLE
        case LLMFailureReason.TIMEOUT:
            return FallbackReason.TIMEOUT
        case LLMFailureReason.REJECTED:
            return FallbackReason.REJECTED
        case _:
            assert_never(reason)


def _reason_for_parse_failure(reason: RCAParseFailureReason) -> FallbackReason:
    """Map the parser's failure onto the recorded reason. Exhaustive on purpose."""
    match reason:
        case RCAParseFailureReason.NOT_JSON:
            return FallbackReason.NOT_JSON
        case RCAParseFailureReason.SCHEMA_INVALID:
            return FallbackReason.SCHEMA_INVALID
        case _:
            assert_never(reason)
