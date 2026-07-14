"""RADAR reasoner agent: the third and final stage of the incident pipeline.

The only stage that calls an LLM, and the only stage that can spend money. It
consumes ``incident.reasoning_requested``, builds a context bundle from the incident
and the investigation plan, asks the model for a root-cause analysis, and stores a
recommendation.

THE PROPERTY THAT DEFINES THIS SERVICE
--------------------------------------
**An incident is never left without a recommendation.** Not when the provider is
down, not when the call times out, not when the model returns something that is not
the JSON it was asked for. Every one of those falls back to a template RCA built
from the plan's own investigation steps — ``is_fallback=true``, ``confidence=low``
— and the engineer still gets a card telling them what to check.

An RCA that says "AI analysis unavailable, here is your checklist" is useful. An
incident that silently produces nothing is not.

STATE COMES FROM THE ROWS, NEVER FROM THE EVENT
-----------------------------------------------
``incident.reasoning_requested`` carries two ids and nothing else. Severity, status,
and alert_count are read from the ``incidents`` row; the steps from the
``investigation_plans`` row. An incident can escalate between being planned and being
reasoned about, and the card an engineer reads must say what the incident IS — not
what it was when the planner happened to look at it.

FALLBACK IS THE ABSENCE OF A CLEAN SUCCESS
------------------------------------------
It is not triggered by a list of known failures — a list is a thing somebody has to
keep complete, and the day it is not, an incident falls through it with no RCA. It is
triggered by anything that is not (a call that succeeded AND an answer that parsed).
``fallback`` matches over the whole result space and ends in ``assert_never``, so a
new failure mode is a **type error**, not a production surprise.

THE FALLBACK CONTRACT
---------------------
A fallback row must not be able to lie about itself, and must not corrupt an
aggregate. So:

    is_fallback        true
    llm_provider       "none"                (no provider served this)
    model_alias        "none"                (NOT "extended" — no model ran, and a
                                              GROUP BY must not count fallbacks as
                                              extended-mode traffic)
    model_id           "template-fallback"

    raw_llm_response   \
    prompt_tokens       >  present iff A CALL COMPLETED — see below. NOT blanket-NULL.
    completion_tokens   |
    latency_ms         /

Those four columns describe **the call**, not the recommendation, and they are NOT
blanket-NULL on a fallback. They are populated together or NULL together, because all
four are read off one ``LLMSuccess | None``.

They are present whenever a call COMPLETED — which includes the case where it came
back with unusable garbage. That call still ran, still took eight seconds, and the
provider still charged us for it. The reasoner is the only stage in RADAR that spends
money, so NULLing those true values would silently under-report the entire cost of the
system. The garbage in ``raw_llm_response`` is also the only evidence of *why* the row
is a template, and the only thing a prompt fix can be tested against.

They are NULL only when **no call completed** — 503 before a response, a timeout, a
rejected request. NULL is therefore a fact ("nothing ran"), never an editorial choice.

The obvious objection — that fallback latencies pollute a p95 of real analyses — is
answered by a column the row already carries:

    WHERE is_fallback = false   -> p95 of successful analyses
    WHERE is_fallback = true    -> how long we waited before giving up

Store the true value; let the reader filter. Destroying a fact to pre-simplify an
aggregate that can already be separated at query time is a trade that only ever loses.

The rest of the debugging information — which mode we asked for, why we fell back, how
long we waited — lives in ``context_bundle``, where it cannot contaminate a provider
or model aggregate.

Layout:

- ``config`` — settings, the Postgres DSN, and the OUTBOUND gateway token.
- ``routes`` — ``POST /events``: the ``processed_events`` gate, then the work.
- ``context`` — the bundle the model is shown, built from the rows.
- ``llm`` — the gateway call. Returns a typed result; never raises.
- ``rca`` — parses the model's answer. Returns a typed result; never raises.
- ``fallback`` — the total match, and the one template generator. THE invariant.
- ``main`` — FastAPI assembly. The inbound agent-token guard (and 401-before-422) is
  the shared one from ``radar_common``.
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["__version__"]
