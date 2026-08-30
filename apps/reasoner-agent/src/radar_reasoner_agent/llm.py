"""The LLM call: one request to the gateway, one hard budget, no exceptions escape.

The reasoner asks the gateway for a root-cause analysis in ``extended`` mode. What
comes back is a *result*, never an exception: either :class:`LLMSuccess` or a typed
:class:`LLMFailure` saying WHY it failed. The caller (the fallback, next commit) needs
the reason — "the provider was down" and "the model took too long" and "our token was
rejected" are three different operational problems, and burying them all in one
``except`` would throw away the only signal that tells them apart.

ONE CLOCK, AND IT CANNOT BE UNDERCUT
------------------------------------
There are two timeouts in reach here, and only one of them may be in charge:

- **httpx's own** (connect / read / write / pool). Its default is **5 seconds**.
- **the reasoner's budget**, an ``asyncio.timeout`` around the whole call, ordered
  against the outbox worker's dispatch timeout in ``radar_common.timeouts``.

If httpx's is the shorter, IT is what actually fires — and every carefully-reasoned
number in ``radar_common.timeouts`` becomes decoration, because a third clock nobody
was thinking about is the one enforcing the bound. An ``extended``-mode call routinely
takes longer than 5 seconds, so with the httpx default every incident would fall back
to a template RCA and the service would look like it was working.

That failure is invisible: no crash, no error, just a fallback rate of 100% and an LLM
that never gets used. So it is made impossible rather than documented:
:class:`GatewayClient` REFUSES TO BE CONSTRUCTED with a client whose timeout could fire
before the budget. A misconfiguration is a startup failure, not a mystery in production.

The client is therefore built with ``timeout=None`` — httpx does not enforce anything,
``asyncio.timeout`` is the single bound, and there is exactly one number to reason
about. A hung connection is still bounded, because the ``asyncio`` timeout wraps the
whole await, not just the read.

WHAT THE MODEL IS SHOWN — AND WHAT IT IS NOT
--------------------------------------------
The user message is RENDERED from the bundle, not dumped from it. The difference
matters because ``retrieved_context`` chunks arrive carrying the knowledge
service's own bookkeeping — ``grade`` (CRAG's per-chunk verdict) and ``status``
(``fixture`` until the corpus has a human review pass) — and neither is
information the model can use well.

Grades in particular were actively harmful — though not for the reason first
supposed, and the difference decides what to do with them next. The grader is
NOT degenerate: across every chunk RADAR has stored, 27 graded ``partial`` and
12 ``sufficient``, and one bundle routinely carries both (3 and 2, in one case).
``sufficient`` is reachable and the grading call earns its keep.

The failure is what happens when a bundle contains no ``sufficient`` chunk at
all. Then "weight excerpts graded sufficient over those graded partial" has
nothing to prefer, and the instruction resolves to *all of your context is the
weaker kind* — so the model answers with the EMPTY-context language ("no runbook
covers this") while looking at a bundle full of the right runbook. Roughly one
bundle in eight is all-``partial``, and in RADAR's stored history that one is
exactly the one that failed: 8 recommendations carried non-empty context, the
single all-``partial`` one produced the empty-context RCA, the other seven did
not. Measured on that bundle against the real gateway: the empty-context
language appeared in 1 of 20 draws pre-fix and 0 of 40 post-fix.

So the grades are a real signal in the pipeline and a hazard in the prompt. What
they are worth to the model is a per-chunk ORDERING; what the prompt turned them
into was a verdict on the context as a whole. Passing them as text is what made
that conversion possible — an ordering can be expressed by the order the chunks
appear in, which is the obvious next move and is deliberately NOT taken here (it
needs its own measurement, and this change is a bug fix).

So the projection is a WHITELIST (:data:`PROMPTED_CHUNK_FIELDS`), not a list of
fields to strip. A blocklist would admit every future chunk field by default, and
the failure mode of admitting one is the one described above: nothing errors,
nothing logs, the RCA just quietly gets worse. Grades are still computed, still
gate inclusion, and still land in ``recommendations.context_bundle`` for the
audit trail — the only thing that changed is that they stop at the prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError
from radar_common import (
    AGENT_TOKEN_HEADER,
    REASONER_LLM_BUDGET_SECONDS,
    ConfigurationError,
    get_logger,
)
from radar_contracts import LLMMode, LLMRequest, LLMResponse, Message

from radar_reasoner_agent.context import ContextBundle

log = get_logger("reasoner.llm")

COMPLETE_PATH = "/v1/complete"

SYSTEM_PROMPT = """\
You are an SRE incident analysis assistant.
You will be given incident metadata, the firing alert's labels and annotations, a
structured investigation plan, retrieved_context (excerpts from this platform's own
runbooks), and RADAR's own history for this alert (historical_prior and
past_feedback).
Respond ONLY with a valid JSON object. No text before or after it.

Schema:
{
  "root_cause": "your best assessment of the likely root cause",
  "confidence": "low|medium|high",
  "recommended_actions": [
    {"order": 1, "action": "specific actionable step"},
    {"order": 2, "action": "specific actionable step"}
  ]
}

Using retrieved_context:
- When it is non-empty, ground your analysis in it: prefer its specific
  thresholds, commands, and procedures over generic advice.
- When it is EMPTY, this platform's runbooks do not cover this incident. That
  is a fact you were given, not a gap to fill: reason from the incident
  metadata and the investigation plan alone, and say in root_cause that no
  runbook covers this. NEVER invent, cite, or allude to a runbook, procedure,
  or excerpt you were not given — an invented runbook reads exactly like a
  real one to the engineer following it at 3am.

Using alert_labels and alert_annotations:
- These carry evidence from the firing alert itself: error breakdowns, deploy
  identifiers, firing metric values, timestamps. Treat them as given facts. When
  they point to a specific cause, ground root_cause in them and raise confidence
  accordingly — a concrete deploy id or a dominant error class is high-confidence
  evidence, not a guess.
- When they are absent or non-specific, do NOT manufacture a cause from them. An
  ambiguous alert with only a generic summary is a medium/low-confidence incident;
  say what the next discriminating signal is rather than committing to one branch.

Using historical_prior and past_feedback (RADAR's own history for this exact alert):
- historical_prior.category_counts is how prior occurrences of THIS alert broke
  down by cause category ("deployment", "dependency", ...), over
  historical_prior.total past recommendations. Treat it as a base rate, not proof:
  when the current evidence is consistent with a category that dominates the prior,
  that agreement is a real reason to raise confidence; when the evidence points
  elsewhere, trust the evidence and say the history did not repeat.
- past_feedback.confirmed_causes are root causes an engineer marked helpful for this
  same alert before. A human-confirmed cause consistent with the current evidence is
  strong support — prefer it and raise confidence. past_feedback.corrections are
  fixes engineers wrote; weigh them the same way.
- past_feedback.unhelpful_count is how often prior RCAs for this alert were rejected.
  A high count is a caution: do not repeat a discredited line of reasoning, and keep
  confidence measured.
- When historical_prior.total is 0, this alert has no history: reason from the
  evidence alone and do NOT invent a base rate.

Rules:
- Do not hallucinate metrics, log lines, or deployment names you were not given.
  The alert_labels/alert_annotations and retrieved_context ARE what you were given;
  anything not in them or the incident metadata is not.
- If you cannot determine a root cause, set confidence=low and explain in root_cause.
- Actions must be specific, not generic (bad: "check logs"): name the exact service,
  signal (log source or metric), time window, and filter to inspect — but draw those
  specifics only from the incident metadata, alert labels, and retrieved_context,
  never ones you supply yourself.
"""
"""The v2 system prompt: v1 plus the retrieved_context rules.

The empty-context rule is the load-bearing addition, and it is what makes the
knowledge pipeline's empty verdict MATTER. CRAG's entire justification is that
it can return nothing when no runbook fits — but that verdict only produces an
honestly-ungrounded RCA if the model treats an empty slot as a fact ("the
runbooks do not cover this") rather than a gap to fill with a plausible
invention. Phase 7's rule against hallucinating metrics and log lines extends
to runbooks for the same reason: fabricated grounding reads exactly like real
grounding to the person acting on it.

The model cannot tell WHY the slot is empty — judged-empty and
retrieval-unavailable look identical in the bundle, deliberately (the model
should reason the same way in both). The stored wrapper's `retrieval` key is
where the system keeps them apart.

It says nothing about GRADES, and must not: the rule it used to carry made an
all-``partial`` bundle read as uniformly low-quality context and drove the model
onto the empty-context path with the right runbook in front of it. See "WHAT THE
MODEL IS SHOWN" in the module docstring — the prompt and
:func:`render_user_message` have to agree, and neither may mention a grade on its
own.

It asks for JSON and nothing else — but a model that returns prose anyway is not an
error the reasoner can prevent, only one it can survive. That is the parser's problem
and the fallback's.
"""


PROMPTED_CHUNK_FIELDS = ("title", "runbook_id", "section", "content")
"""The ONLY ``retrieved_context`` keys the model is shown.

A whitelist on purpose. The chunks arrive from the knowledge service's context API
carrying ``grade`` and ``status`` alongside these four, and a blocklist ("drop
grade, drop status") would admit whatever field that API grows next — silently,
into the prompt, with no test and no log line to notice it. Here, a new field
reaches the model only when somebody adds it to this tuple and says why.

Order is the reading order in the rendered JSON: what the excerpt is, where it
came from, and then the text itself.
"""


def render_user_message(bundle: ContextBundle) -> str:
    """The user message: the bundle, with each retrieved chunk projected down.

    Every non-chunk field goes through verbatim — the model needs the live
    severity, the alert name, the plan's steps. Each chunk is reduced to
    :data:`PROMPTED_CHUNK_FIELDS`, which is what stops the pipeline's grading
    bookkeeping from reaching the prompt; the module docstring has the incident
    that made this necessary.

    This is the ONE place the prompt's user half is built. It is a named function
    rather than an inline dump so the "no grades in the prompt" property is
    testable without a gateway, an incident, or a model.
    """
    payload = bundle.model_dump(mode="json")
    chunks: list[dict[str, Any]] = payload["retrieved_context"]
    payload["retrieved_context"] = [_prompted_fields(chunk) for chunk in chunks]
    # indent=2, matching what the bundle used to be dumped as: the model reads
    # this, and a wall of single-line JSON is measurably harder to ground in.
    return json.dumps(payload, indent=2)


def _prompted_fields(chunk: dict[str, Any]) -> dict[str, Any]:
    """One chunk, reduced to the fields the model sees.

    A missing field is dropped rather than raised on — the reasoner's whole
    contract is that an incident gets an RCA, and dying over a malformed chunk
    would trade a grounded analysis for a dead-lettered event. But it is not
    dropped SILENTLY: the reasoner does not validate the context API's chunk
    shape (see ``knowledge._ContextResponse``), so a chunk arriving without
    ``content`` means the model is being shown an empty excerpt, and that is a
    real defect upstream that would otherwise look like the model ignoring its
    context.
    """
    missing = [name for name in PROMPTED_CHUNK_FIELDS if name not in chunk]
    if missing:
        log.warning(
            "llm.chunk_missing_prompted_fields",
            missing=missing,
            runbook_id=chunk.get("runbook_id"),
        )
    return {name: chunk[name] for name in PROMPTED_CHUNK_FIELDS if name in chunk}


class LLMFailureReason(StrEnum):
    """WHY the call failed. Three different operational problems, kept apart."""

    #: The gateway could not serve the request: every provider failed (503), the
    #: gateway itself errored (5xx), or we could not reach it at all. Nothing is wrong
    #: with RADAR — the LLM is simply unavailable, which is what the fallback is for.
    GATEWAY_UNAVAILABLE = "gateway_unavailable"

    #: The call outran the reasoner's budget. The model may still be generating; we
    #: stopped waiting. Distinguished from the above because it means the LLM is UP
    #: and slow, which is a capacity problem, not an outage.
    TIMEOUT = "timeout"

    #: The gateway rejected US: a bad token (401), a mode this token may not use
    #: (403), or a request it will not accept (422). This is OUR misconfiguration, it
    #: will fail identically on every incident, and it must be loud — a fallback rate
    #: of 100% with this reason means the reasoner has never once used the LLM.
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class LLMSuccess:
    """A completion came back. Whether it is USABLE is the parser's question."""

    content: str
    provider: str
    model: str
    mode: str
    prompt_tokens: int
    completion_tokens: int | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LLMFailure:
    """No completion. ``reason`` is recorded on the fallback's context bundle."""

    reason: LLMFailureReason
    detail: str
    elapsed_ms: int


LLMResult = LLMSuccess | LLMFailure


class GatewayClient:
    """Calls ``POST /v1/complete`` in ``extended`` mode, within a hard budget.

    Refuses construction if ``client``'s own timeout could fire before ``budget`` — see
    the module docstring. That is the single most important line in this file: without
    it, httpx's 5-second default silently becomes the real bound and the LLM is never
    used at all.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        budget_seconds: float = REASONER_LLM_BUDGET_SECONDS,
    ) -> None:
        if budget_seconds <= 0:
            raise ConfigurationError("the LLM budget must be greater than zero")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._budget = budget_seconds

    async def complete(self, bundle: ContextBundle) -> LLMResult:
        """Ask the model for an RCA. Returns a result; never raises."""
        request = LLMRequest(
            mode=LLMMode.EXTENDED,
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=render_user_message(bundle)),
            ],
        )
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            # THE bound. httpx enforces nothing (timeout=None), so this is the only
            # clock, and it is the one ordered against the worker's dispatch timeout.
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    COMPLETE_PATH,
                    json=request.model_dump(mode="json"),
                    headers=headers,
                )
        except TimeoutError:
            return self._failure(
                LLMFailureReason.TIMEOUT,
                f"the LLM did not answer within {self._budget:g}s",
                started,
            )
        except httpx.TimeoutException:
            # Should be unreachable: the constructor rejects a client whose timeout
            # could fire first. If it happens, httpx is enforcing a bound nobody chose
            # — say so, rather than quietly reporting it as our timeout.
            return self._failure(
                LLMFailureReason.TIMEOUT,
                "httpx enforced a timeout shorter than the budget; the client is "
                "misconfigured and the LLM budget is not the bound in force",
                started,
            )
        except httpx.HTTPError as exc:
            return self._failure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"cannot reach the llm-gateway: {type(exc).__name__}",
                started,
            )

        return self._classify(response, started)

    def _classify(self, response: httpx.Response, started: float) -> LLMResult:
        elapsed = _elapsed_ms(started)

        if response.status_code == 503:
            # The gateway's own signal that every provider failed (ADR 0004's fallback
            # chain is exhausted). This is THE case the template fallback exists for.
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                "every LLM provider failed (gateway returned 503)",
                elapsed,
            )
        if response.status_code in (401, 403, 422):
            # OUR problem, and it will recur on every incident until someone fixes it.
            return LLMFailure(
                LLMFailureReason.REJECTED,
                f"the gateway rejected this request: HTTP {response.status_code}",
                elapsed,
            )
        if response.status_code >= 500 or response.status_code == 429:
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"the gateway is unhealthy: HTTP {response.status_code}",
                elapsed,
            )
        if response.status_code != 200:
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"unexpected gateway status: HTTP {response.status_code}",
                elapsed,
            )

        try:
            parsed = LLMResponse.model_validate_json(response.content)
        except ValidationError:
            # The gateway answered 200 with something that is not the gateway's own
            # contract. Not the model's fault and not parseable — treat the gateway as
            # unavailable rather than blame the RCA parser for a shape it never saw.
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                "the gateway returned 200 with a body that is not an LLMResponse",
                elapsed,
            )

        return LLMSuccess(
            content=parsed.content,
            provider=parsed.provider,
            model=parsed.model,
            mode=parsed.mode.value,
            prompt_tokens=parsed.usage.prompt_tokens,
            completion_tokens=parsed.usage.completion_tokens,
            # The gateway measures its own latency; prefer it over ours, which includes
            # our network hop to the gateway.
            latency_ms=parsed.latency_ms,
        )

    def _failure(
        self, reason: LLMFailureReason, detail: str, started: float
    ) -> LLMFailure:
        failure = LLMFailure(reason, detail, _elapsed_ms(started))
        # WARNING, not error: a provider outage is not a RADAR bug, and the incident
        # still gets an RCA. REJECTED is the one that means something is broken here.
        log.warning(
            "llm.call_failed",
            reason=failure.reason.value,
            detail=failure.detail,
            elapsed_ms=failure.elapsed_ms,
        )
        return failure


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the reasoner's budget.

    httpx defaults to 5 seconds. An ``extended``-mode call routinely takes longer, so a
    client left on the default would kill every call at 5s, every incident would fall
    back to a template RCA, and the service would look perfectly healthy while never
    once using the model it exists to use. There would be no error to find.

    So the two clocks are not allowed to disagree: either httpx enforces nothing
    (``timeout=None``, the intended configuration) or its timeout is at least the
    budget. Anything else fails at startup, where it is a config error somebody can
    read.
    """
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than the "
                f"LLM budget ({budget:g}s), so httpx — not the budget — would be the "
                "bound actually in force. Build the client with timeout=None and let "
                "asyncio.timeout own the clock."
            )
