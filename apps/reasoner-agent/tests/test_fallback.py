"""The invariant: an incident ALWAYS ends with a recommendation.

These tests are the reason the service exists. Everything else in the reasoner is
machinery for getting a *good* RCA; this is the part that guarantees the engineer gets
*an* RCA — and it is the part that only runs when things are already going badly, which
is exactly why it is tested hardest.

WHY THESE TESTS ITERATE ENUMS INSTEAD OF LISTING CASES
------------------------------------------------------
Several tests below are parametrized over ``list(LLMFailureReason)`` and
``list(RCAParseFailureReason)`` rather than over a hand-written list of reasons. That is
deliberate and it is the whole trick: a hand-written list of triggers is a list somebody
has to keep complete, and the original R6 spec's own trigger list was already missing
``REJECTED``. Driving the parametrization off the enum means **adding a failure mode
automatically adds a test case for it** — and that case fails, loudly, until someone
decides what the new mode means.

Between the enum-driven parametrization here and ``assert_never`` in
``fallback.resolve``, a new failure mode cannot reach production un-handled: mypy
refuses to compile it, and the test suite refuses to pass it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from _pytest.mark import ParameterSet
from pydantic import ValidationError
from radar_contracts import Confidence, LLMMode, PlanStep, Severity
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.fallback import (
    FALLBACK_MODEL_ALIAS,
    FALLBACK_MODEL_ID,
    FALLBACK_PROVIDER,
    FallbackReason,
    ReasoningOutcome,
    generate_template_rca,
    resolve,
)
from radar_reasoner_agent.llm import LLMFailure, LLMFailureReason, LLMResult, LLMSuccess
from radar_reasoner_agent.rca import RCAParseFailureReason

STEPS = [
    PlanStep(order=1, description="Check recent deployments for order-service"),
    PlanStep(order=2, description="Review error logs in Kibana for the last 30m"),
    PlanStep(order=3, description="Roll back if the deploy correlates"),
]

GOOD_RCA = json.dumps(
    {
        "root_cause": "A bad deploy to order-service broke order validation.",
        "confidence": "high",
        "recommended_actions": [
            {"order": 1, "action": "kubectl rollout undo deployment/order-service"},
        ],
    }
)


def _bundle(*, steps: list[PlanStep] | None = None) -> ContextBundle:
    return ContextBundle(
        incident_id=uuid4(),
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
        severity=Severity.CRITICAL,
        opened_at=datetime.now(UTC),
        alert_count=3,
        investigation_steps=STEPS if steps is None else steps,
        retrieved_context=[],
    )


def _success(content: str) -> LLMSuccess:
    return LLMSuccess(
        content=content,
        provider="openai",
        model="gpt-4o",
        mode=LLMMode.EXTENDED.value,
        prompt_tokens=420,
        completion_tokens=99,
        latency_ms=8_500,
    )


def _failure(reason: LLMFailureReason) -> LLMFailure:
    return LLMFailure(
        reason=reason, detail=f"simulated {reason.value}", elapsed_ms=1234
    )


#: Content that reaches the parser and comes back unusable — one per parse failure mode.
UNPARSEABLE: dict[RCAParseFailureReason, str] = {
    RCAParseFailureReason.NOT_JSON: (
        "I'm sorry, I can't determine the root cause from the information given."
    ),
    RCAParseFailureReason.SCHEMA_INVALID: json.dumps(
        # Valid JSON. Not an RCA: no actions, and a confidence we cannot compare.
        {
            "root_cause": "Something broke.",
            "confidence": "very high",
            "recommended_actions": [],
        }
    ),
}


def _every_failing_result() -> list[ParameterSet]:
    """Every way reasoning can fail to produce a clean analysis. Driven by the enums.

    NOT a hand-written list — see the module docstring. A new ``LLMFailureReason`` or
    ``RCAParseFailureReason`` lands here automatically, and every test below that is
    parametrized on this immediately covers it.
    """
    cases = [
        pytest.param(_failure(reason), id=f"llm-{reason.value}")
        for reason in LLMFailureReason
    ]
    cases += [
        pytest.param(_success(UNPARSEABLE[reason]), id=f"parse-{reason.value}")
        for reason in RCAParseFailureReason
    ]
    return cases


# --- THE INVARIANT: every failure produces a recommendation -------------------


@pytest.mark.parametrize("result", _every_failing_result())
def test_every_failure_produces_a_fallback_recommendation(result: LLMResult) -> None:
    """No path through the reasoner leaves an incident un-recommended.

    Mutation that must turn this red: make any arm of ``resolve`` raise (or return
    ``None``) instead of falling back. Deleting an arm outright does not even get this
    far — ``assert_never`` makes it a mypy error, which is the stronger failure.
    """
    outcome = resolve(_bundle(), result)

    assert isinstance(outcome, ReasoningOutcome)
    assert outcome.is_fallback is True
    assert outcome.root_cause  # non-empty: there is something to show the engineer
    assert outcome.recommended_actions  # and something for them to DO
    assert outcome.confidence is Confidence.LOW


@pytest.mark.parametrize("result", _every_failing_result())
def test_every_failure_records_why_it_fell_back(result: LLMResult) -> None:
    """``fallback_reason`` and ``attempted_mode`` are recorded on the context bundle.

    The reason is RECORDED, never branched on — but a reason that is not written down
    is a fallback nobody can diagnose. ``rejected`` in particular must be separable from
    ``gateway_unavailable``: one means fix your config, the other means wait.
    """
    outcome = resolve(_bundle(), result)

    metadata = outcome.context_bundle.fallback
    assert metadata is not None
    assert metadata.reason in set(FallbackReason)
    assert metadata.attempted_mode is LLMMode.EXTENDED
    assert metadata.detail  # the failure's own words, not an empty string


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (LLMFailureReason.GATEWAY_UNAVAILABLE, FallbackReason.GATEWAY_UNAVAILABLE),
        (LLMFailureReason.TIMEOUT, FallbackReason.TIMEOUT),
        (LLMFailureReason.REJECTED, FallbackReason.REJECTED),
    ],
)
def test_each_llm_failure_maps_to_its_own_reason(
    reason: LLMFailureReason, expected: FallbackReason
) -> None:
    """The four triggers are told apart, not collapsed into "it failed".

    ``rejected`` is the one that matters most here: it is OUR misconfiguration, it will
    fail identically on every incident, and a fallback rate of 100% carrying this reason
    means the reasoner has never once used the LLM while looking perfectly healthy.
    Alerting can only see that if the reason survives to the row.
    """
    outcome = resolve(_bundle(), _failure(reason))

    assert outcome.context_bundle.fallback is not None
    assert outcome.context_bundle.fallback.reason is expected


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (RCAParseFailureReason.NOT_JSON, FallbackReason.NOT_JSON),
        (RCAParseFailureReason.SCHEMA_INVALID, FallbackReason.SCHEMA_INVALID),
    ],
)
def test_each_parse_failure_maps_to_its_own_reason(
    reason: RCAParseFailureReason, expected: FallbackReason
) -> None:
    outcome = resolve(_bundle(), _success(UNPARSEABLE[reason]))

    assert outcome.context_bundle.fallback is not None
    assert outcome.context_bundle.fallback.reason is expected


def test_a_reason_exists_for_every_upstream_failure_mode() -> None:
    """Every source failure mode has a ``FallbackReason``, and the mapping is total.

    Guards the shortcut ``fallback.py`` deliberately refuses to take:
    ``FallbackReason(failure.reason.value)`` would work today — the strings match — and
    would raise ``ValueError`` at 3am the day someone adds a fourth
    ``LLMFailureReason``. This test fails at build time instead.
    """
    reasons = {r.value for r in FallbackReason}
    for llm_reason in LLMFailureReason:
        assert llm_reason.value in reasons, f"no FallbackReason for {llm_reason}"
    for parse_reason in RCAParseFailureReason:
        assert parse_reason.value in reasons, f"no FallbackReason for {parse_reason}"


# --- is_fallback <=> provider agreement, in BOTH directions -------------------


@pytest.mark.parametrize("result", _every_failing_result())
def test_a_fallback_row_always_names_no_provider(result: LLMResult) -> None:
    """``is_fallback=true`` ALWAYS coincides with ``provider="none"``.

    A row that says it is a template while naming a provider lies about itself, and a
    ``GROUP BY provider`` over it double-counts traffic that never happened. The two
    fields are set together, in one place, so no code path can produce the disagreement.
    """
    outcome = resolve(_bundle(), result)

    assert outcome.is_fallback is True
    assert outcome.llm_provider == FALLBACK_PROVIDER == "none"
    assert outcome.model_alias == FALLBACK_MODEL_ALIAS == "none"
    assert outcome.model_id == FALLBACK_MODEL_ID == "template-fallback"
    # NOT "extended": no model ran, and a GROUP BY model_alias must not count this as
    # extended-mode traffic.
    assert outcome.model_alias != LLMMode.EXTENDED.value


def test_a_real_analysis_never_names_no_provider() -> None:
    """The other direction: ``is_fallback=false`` NEVER has ``provider="none"``.

    Both directions are tested because only one of them is the interesting bug.
    Asserting that fallbacks say "none" is easy and catches nothing; asserting that a
    REAL analysis cannot claim "none" is what stops a genuine RCA from being silently
    filtered out of every dashboard that excludes fallback traffic.
    """
    outcome = resolve(_bundle(), _success(GOOD_RCA))

    assert outcome.is_fallback is False
    assert outcome.llm_provider != FALLBACK_PROVIDER
    assert outcome.llm_provider == "openai"
    assert outcome.model_id == "gpt-4o"
    assert outcome.model_alias == LLMMode.EXTENDED.value
    assert outcome.context_bundle.fallback is None


@pytest.mark.parametrize(
    "result",
    [*_every_failing_result(), pytest.param(_success(GOOD_RCA), id="clean-success")],
)
def test_is_fallback_and_provider_can_never_disagree(result: LLMResult) -> None:
    """The biconditional, over the whole space: is_fallback <=> provider is "none".

    Mutation that must turn this red: set ``llm_provider=success.provider`` on the
    fallback path, or ``is_fallback=True`` on the success path — any edit that lets the
    two fields drift apart.
    """
    outcome = resolve(_bundle(), result)

    assert outcome.is_fallback == (outcome.llm_provider == FALLBACK_PROVIDER)


# --- the template is the plan, and there is only one generator ----------------


@pytest.mark.parametrize("result", _every_failing_result())
def test_the_template_is_built_from_the_plans_own_steps(result: LLMResult) -> None:
    """Every trigger yields the SAME template — the incident's investigation plan.

    One generator, not three. Three copies would drift, and the copy that drifted would
    be the one on the rare path nobody looks at.
    """
    bundle = _bundle()

    outcome = resolve(bundle, result)

    assert [(a.order, a.action) for a in outcome.recommended_actions] == [
        (s.order, s.description) for s in STEPS
    ]
    assert (
        outcome.recommended_actions == generate_template_rca(bundle).recommended_actions
    )
    assert bundle.service_name in outcome.root_cause
    assert bundle.alert_name in outcome.root_cause


def test_the_template_does_not_blame_an_outage_it_cannot_diagnose() -> None:
    """The card says the analysis is unavailable. It does NOT say the provider is down.

    "AI analysis unavailable due to LLM provider outage" is FALSE on three of the five
    reasons — the provider is emphatically not down when it returns prose instead of
    JSON, or when it rejects our token. A card that misdiagnoses its own failure sends
    the engineer to check the wrong thing. The precise reason lives in the metadata,
    where it is true.
    """
    root_cause = generate_template_rca(_bundle()).root_cause.lower()

    assert "unavailable" in root_cause
    assert "outage" not in root_cause


def test_actions_are_ordered_even_if_the_stored_steps_are_not() -> None:
    """Steps come out of a JSONB column; their array order is not a promise."""
    shuffled = [
        PlanStep(order=3, description="third"),
        PlanStep(order=1, description="first"),
        PlanStep(order=2, description="second"),
    ]

    actions = generate_template_rca(_bundle(steps=shuffled)).recommended_actions

    assert [a.order for a in actions] == [1, 2, 3]
    assert [a.action for a in actions] == ["first", "second", "third"]


# --- the fallback cannot be the thing that breaks the invariant ---------------


def test_a_plan_with_no_steps_still_produces_a_recommendation() -> None:
    """The one input that could make the FALLBACK ITSELF raise, and does not.

    ``ParsedRCA`` requires at least one action. ``investigation_steps`` comes from a
    JSONB column with no floor on it, so an empty plan is representable — and a
    generator that mapped steps to actions one-for-one would raise ``ValidationError``
    here. That exception would escape the handler, dead-letter the event, and leave the
    incident with NO recommendation: the exact outcome this module exists to prevent,
    reached through the code that prevents it.

    The planner's template loader enforces ``min_length=1``, so this should be
    unreachable. "Should be" is not a property. The invariant does not rest on another
    service's validator holding.

    Mutation that must turn this red: delete the ``if not actions`` floor in
    ``generate_template_rca``.
    """
    bundle = _bundle(steps=[])

    outcome = resolve(bundle, _failure(LLMFailureReason.GATEWAY_UNAVAILABLE))

    assert outcome.is_fallback is True
    assert len(outcome.recommended_actions) == 1
    assert outcome.recommended_actions[0].order == 1
    assert outcome.recommended_actions[0].action  # non-empty: something to actually do


@pytest.mark.parametrize("result", _every_failing_result())
def test_resolve_never_raises_on_a_plan_with_no_steps(result: LLMResult) -> None:
    """Not one trigger, but every trigger, against the empty plan."""
    outcome = resolve(_bundle(steps=[]), result)

    assert outcome.is_fallback is True
    assert outcome.recommended_actions


# --- raw_llm_response means "what the model literally returned" ----------------


@pytest.mark.parametrize("reason", list(RCAParseFailureReason))
def test_unusable_model_output_is_kept_for_debugging(
    reason: RCAParseFailureReason,
) -> None:
    """When the model DID answer, the unusable answer is stored. It is the evidence.

    This is the artifact that explains why the row is a template, and the thing any
    prompt fix has to be tested against. Discarding it to keep fallback rows uniform
    would buy tidiness with the only clue.
    """
    content = UNPARSEABLE[reason]

    outcome = resolve(_bundle(), _success(content))

    assert outcome.is_fallback is True
    assert outcome.raw_llm_response == content


@pytest.mark.parametrize("reason", list(LLMFailureReason))
def test_no_response_body_means_no_raw_response(reason: LLMFailureReason) -> None:
    """503, timeout, rejected: there was no response, so the column is NULL.

    NULL means "the model returned nothing", not "we chose not to keep it". The
    distinction is the column's entire meaning.
    """
    outcome = resolve(_bundle(), _failure(reason))

    assert outcome.is_fallback is True
    assert outcome.raw_llm_response is None


@pytest.mark.parametrize("reason", list(RCAParseFailureReason))
def test_a_wasted_call_still_reports_what_it_cost(
    reason: RCAParseFailureReason,
) -> None:
    """The model ran, returned garbage, and the provider CHARGED us. Record the spend.

    This reverses the contract R6 shipped. NULLing tokens here would silently
    under-report the only money RADAR spends — the reasoner is the only stage that
    spends any — and the call was every bit as expensive as one that produced a usable
    answer. The row is a template; the invoice is real.

    Latency follows the same rule, not a separate decision: the call genuinely took this
    long. The "it pollutes p95" objection is answered by ``is_fallback``, which the row
    carries — filter on it at query time rather than destroying a true value up front.

    Mutation that must turn this red: set ``call=None`` on the parse-failure arm of
    ``resolve``.
    """
    outcome = resolve(_bundle(), _success(UNPARSEABLE[reason]))

    assert outcome.is_fallback is True
    assert outcome.prompt_tokens == 420
    assert outcome.completion_tokens == 99
    assert outcome.latency_ms == 8_500


@pytest.mark.parametrize("reason", list(LLMFailureReason))
def test_a_call_that_never_completed_reports_no_figures(
    reason: LLMFailureReason,
) -> None:
    """503, timeout, rejected: no call completed, so there is nothing to report.

    NULL is a FACT here — "nothing ran, nothing was spent" — not an editorial choice
    about what to keep. That is the whole difference from the case above.
    """
    outcome = resolve(_bundle(), _failure(reason))

    assert outcome.is_fallback is True
    assert outcome.prompt_tokens is None
    assert outcome.completion_tokens is None
    assert outcome.latency_ms is None


@pytest.mark.parametrize(
    "result",
    [*_every_failing_result(), pytest.param(_success(GOOD_RCA), id="clean-success")],
)
def test_the_call_columns_are_all_present_or_all_absent(result: LLMResult) -> None:
    """The four call-describing columns can never disagree, over the whole result space.

    A row saying the model returned 900 bytes of text but cost zero tokens would be
    lying about one of the two, and there would be no way to tell which. They are all
    read off a single ``LLMSuccess | None``, so there is no code path that can populate
    one and forget another — the property is structural.

    Mutation that must turn this red: give ``_from_failure`` a separate
    ``raw_llm_response`` argument again and let it drift from the token fields.
    """
    outcome = resolve(_bundle(), result)

    present = [
        outcome.raw_llm_response is not None,
        outcome.prompt_tokens is not None,
        outcome.latency_ms is not None,
    ]
    assert len(set(present)) == 1, "the call columns disagree about whether a call ran"

    # And "a call ran" is exactly "the model returned something we can point at".
    a_call_completed = outcome.raw_llm_response is not None
    assert a_call_completed == (outcome.prompt_tokens is not None)


def test_a_real_analysis_reports_its_real_figures() -> None:
    outcome = resolve(_bundle(), _success(GOOD_RCA))

    assert outcome.raw_llm_response == GOOD_RCA
    assert outcome.prompt_tokens == 420
    assert outcome.completion_tokens == 99
    assert outcome.latency_ms == 8_500
    assert outcome.confidence is Confidence.HIGH
    assert outcome.root_cause == "A bad deploy to order-service broke order validation."


# --- the stored bundle keeps the prompt honest --------------------------------


@pytest.mark.parametrize("result", _every_failing_result())
def test_the_stored_bundle_nests_what_the_model_was_shown_verbatim(
    result: LLMResult,
) -> None:
    """The wrapper COMPOSES; it does not merge.

    The bundle is stored byte-for-byte as the model saw it, with our metadata in a
    sibling key. Flattening them would make "what was sent to the model" and "what we
    added afterwards" indistinguishable — and reconstructing what the model saw is the
    entire reason this column exists.
    """
    bundle = _bundle()

    outcome = resolve(bundle, result)

    stored: dict[str, Any] = outcome.context_bundle.model_dump(mode="json")
    assert stored["bundle"] == bundle.model_dump(mode="json")
    assert stored["fallback"]["attempted_mode"] == LLMMode.EXTENDED.value
    assert stored["fallback"]["reason"] in {r.value for r in FallbackReason}


def test_the_prompt_facing_bundle_cannot_carry_fallback_fields() -> None:
    """``ContextBundle`` is serialized straight into the prompt, so it stays clean.

    Putting ``fallback_reason`` on it would be putting it in front of the model — on
    the SUCCESS path, where there is no fallback at all. It is ``extra="forbid"``, so
    this is enforced, not merely intended.
    """
    assert "fallback" not in ContextBundle.model_fields
    assert "fallback_reason" not in ContextBundle.model_fields
    assert "attempted_mode" not in ContextBundle.model_fields

    with pytest.raises(ValidationError):
        ContextBundle(
            incident_id=uuid4(),
            service_name="order-service",
            alert_name="OrderProcessingFailureRate",
            severity=Severity.CRITICAL,
            opened_at=datetime.now(UTC),
            alert_count=1,
            investigation_steps=STEPS,
            retrieved_context=[],
            fallback_reason="gateway_unavailable",  # type: ignore[call-arg]
        )
